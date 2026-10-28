"""
Массовая конвертация изображений в JPG.

Поддерживаемые форматы:
    HEIC, HEIF, WebP, AVIF, JXL, PNG, JPEG, BMP, TIFF, GIF

Особенности:
    * определяет реальный формат по сигнатуре (а не по расширению);
    * умеет пропускать мусорный префикс перед изображением
      (например, \\r\\n в начале .webp);
    * применяет EXIF-поворот;
    * прозрачность кладёт на белый фон;
    * для анимированных берёт первый кадр;
    * результат — ZIP с JPG в папке «Загрузки».

Лог: %TEMP%\\image_to_jpg.log
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageOps, UnidentifiedImageError

# --- Регистрация плагинов для «экзотических» форматов ---------------------

_HEIF_OK = _AVIF_OK = _JXL_OK = False

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIF_OK = True
except Exception:  # noqa: BLE001
    pass

try:
    import pillow_avif  # noqa: F401
    _AVIF_OK = True
except Exception:  # noqa: BLE001
    pass

try:
    import pillow_jxl  # noqa: F401
    _JXL_OK = True
except Exception:  # noqa: BLE001
    pass


IMAGE_PATTERNS = (
    '*.heic', '*.heif',
    '*.webp',
    '*.avif',
    '*.jxl',
    '*.png', '*.jpg', '*.jpeg',
    '*.bmp', '*.tif', '*.tiff',
    '*.gif',
)

LOG_PATH = Path(tempfile.gettempdir()) / 'image_to_jpg.log'
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8',
)
log = logging.getLogger(__name__)


# --- Утилиты ---------------------------------------------------------------

def get_downloads_dir() -> Path:
    downloads = Path.home() / 'Downloads'
    return downloads if downloads.exists() else Path.cwd()


def open_path(path: Path) -> None:
    try:
        if sys.platform.startswith('win'):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == 'darwin':
            subprocess.run(['open', str(path)], check=False)
        else:
            subprocess.run(['xdg-open', str(path)], check=False)
    except Exception as e:  # noqa: BLE001
        log.warning('Не удалось открыть %s: %s', path, e)


# --- Определение формата по сигнатуре -------------------------------------

_IMAGE_STARTS = [
    (b'\xff\xd8\xff', 'JPEG'),
    (b'\x89PNG\r\n\x1a\n', 'PNG'),
    (b'GIF87a', 'GIF'),
    (b'GIF89a', 'GIF'),
    (b'BM', 'BMP'),
    (b'II*\x00', 'TIFF'),
    (b'MM\x00*', 'TIFF'),
    (b'\x00\x00\x00\x0cJXL \r\n\x87\n', 'JXL'),
]


def detect_format(path: Path, scan: int = 4096) -> str | None:
    """Определяет формат по «магическим байтам» в первых `scan` байтах."""
    try:
        with open(path, 'rb') as f:
            data = f.read(scan)
    except OSError:
        return None

    # WEBP: 'RIFF' .... 'WEBP'
    pos = data.find(b'RIFF')
    while pos >= 0:
        if data[pos + 8:pos + 12] == b'WEBP':
            return 'WEBP'
        pos = data.find(b'RIFF', pos + 1)

    # ISO BMFF (HEIC/HEIF/AVIF)
    pos = data.find(b'ftyp')
    while pos >= 4:
        brand = data[pos + 4:pos + 8]
        if brand in (b'heic', b'heix', b'hevc', b'hevx',
                     b'heim', b'heis', b'hevm', b'hevs',
                     b'mif1', b'msf1'):
            return 'HEIF'
        if brand in (b'avif', b'avis'):
            return 'AVIF'
        pos = data.find(b'ftyp', pos + 4)

    for sig, fmt in _IMAGE_STARTS:
        if data.find(sig) >= 0:
            return fmt

    return None


def _find_image_start(data: bytes, scan: int = 4096) -> tuple[int, str] | None:
    """Ищет смещение реального начала изображения в первых `scan` байтах."""
    window = data[:scan]

    pos = window.find(b'RIFF')
    while pos >= 0:
        if data[pos + 8:pos + 12] == b'WEBP':
            return pos, 'WEBP'
        pos = window.find(b'RIFF', pos + 1)

    pos = window.find(b'ftyp')
    while pos >= 4:
        brand = data[pos + 4:pos + 8]
        if brand in (b'heic', b'heix', b'hevc', b'hevx',
                     b'heim', b'heis', b'hevm', b'hevs',
                     b'mif1', b'msf1'):
            return pos - 4, 'HEIF'
        if brand in (b'avif', b'avis'):
            return pos - 4, 'AVIF'
        pos = window.find(b'ftyp', pos + 4)

    for sig, fmt in _IMAGE_STARTS:
        p = window.find(sig)
        if p >= 0:
            return p, fmt

    return None


def _open_image_smart(src: Path) -> Image.Image:
    """Image.open с пропуском мусорного префикса перед изображением."""
    data = src.read_bytes()

    try:
        return Image.open(BytesIO(data))
    except (UnidentifiedImageError, OSError):
        pass

    found = _find_image_start(data)
    if found:
        pos, fmt = found
        if pos > 0:
            log.warning(
                '%s: пропущено %d байт мусора перед %s (первые байты: %s)',
                src.name, pos, fmt, data[:pos][:16].hex(' '),
            )
            try:
                return Image.open(BytesIO(data[pos:]))
            except (UnidentifiedImageError, OSError):
                pass

    raise UnidentifiedImageError(f'cannot identify image file {str(src)!r}')


# --- Нормализация и сохранение --------------------------------------------

def _normalize_image(image: Image.Image) -> Image.Image:
    """Приводит изображение к RGB: прозрачность на белый фон, EXIF-поворот,
    анимация — первый кадр."""
    if getattr(image, 'is_animated', False):
        try:
            image.seek(0)
        except Exception:  # noqa: BLE001
            pass

    try:
        image = ImageOps.exif_transpose(image)
    except Exception as e:  # noqa: BLE001
        log.warning('exif_transpose failed: %s', e)

    mode = image.mode

    if mode in ('RGBA', 'LA') or (mode == 'P' and 'transparency' in image.info):
        rgba = image.convert('RGBA')
        background = Image.new('RGB', rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel('A'))
        return background

    if mode != 'RGB':
        try:
            return image.convert('RGB')
        except Exception as e:  # noqa: BLE001
            log.warning('convert(%s -> RGB) failed: %s', mode, e)
            return image.convert('RGB')

    return image


def _unique_path(folder: Path, stem: str, suffix: str = '.jpg') -> Path:
    candidate = folder / f'{stem}{suffix}'
    i = 1
    while candidate.exists():
        candidate = folder / f'{stem}_{i}{suffix}'
        i += 1
    return candidate


# --- Основная конвертация --------------------------------------------------

def convert_images(
    image_paths: list[Path],
    quality: int = 90,
) -> tuple[list[Path], list[str]]:
    tmp_dir = Path(tempfile.mkdtemp(prefix='img2jpg_'))
    jpg_files: list[Path] = []
    errors: list[str] = []

    for src in image_paths:
        try:
            fmt = detect_format(src)
            log.info('Обработка %s (формат по сигнатуре: %s)', src.name, fmt)

            if fmt == 'HEIF' and not _HEIF_OK:
                raise RuntimeError('файл HEIF/HEIC, но плагин pillow-heif не установлен')
            if fmt == 'AVIF' and not _AVIF_OK:
                raise RuntimeError('файл AVIF, но плагин pillow-avif-plugin не установлен')
            if fmt == 'JXL' and not _JXL_OK:
                raise RuntimeError('файл JPEG XL, но плагин pillow-jxl-plugin не установлен')

            with _open_image_smart(src) as image:
                rgb = _normalize_image(image)
                out_path = _unique_path(tmp_dir, src.stem)
                rgb.save(out_path, 'JPEG', quality=quality, optimize=True)

            jpg_files.append(out_path)

        except Exception as e:  # noqa: BLE001
            fmt = detect_format(src)
            hint = f' (по сигнатуре: {fmt})' if fmt else ''
            log.exception('Ошибка конвертации %s', src)
            errors.append(f'{src.name}: {e}{hint}')

    return jpg_files, errors


def make_zip(jpg_files: list[Path], output_zip: Path) -> None:
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for jpg in jpg_files:
            zf.write(jpg, arcname=jpg.name)


def _cleanup_tmp(jpg_files: list[Path]) -> None:
    for jpg in jpg_files:
        try:
            jpg.unlink()
        except OSError:
            pass
    if jpg_files:
        try:
            jpg_files[0].parent.rmdir()
        except OSError:
            pass


# --- GUI -------------------------------------------------------------------

def main() -> None:
    log.info('=== Запуск image→jpg ===')
    log.info('Плагины: HEIF=%s AVIF=%s JXL=%s', _HEIF_OK, _AVIF_OK, _JXL_OK)

    root = tk.Tk()
    root.withdraw()

    selected = filedialog.askopenfilenames(
        title='Выберите изображения для конвертации в JPG',
        filetypes=[
            ('Все поддерживаемые', ' '.join(IMAGE_PATTERNS)),
            ('HEIC / HEIF', '*.heic *.heif'),
            ('WebP', '*.webp'),
            ('AVIF', '*.avif'),
            ('JPEG XL', '*.jxl'),
            ('Обычные (PNG, JPG, BMP, TIFF, GIF)',
             '*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif'),
            ('Все файлы', '*.*'),
        ],
    )

    if not selected:
        log.info('Пользователь отменил выбор')
        return

    image_paths = [Path(p) for p in selected]
    log.info('Выбрано файлов: %d', len(image_paths))

    jpg_files, errors = convert_images(image_paths, quality=90)

    if not jpg_files:
        details = '\n'.join(errors) if errors else 'Неизвестная ошибка.'
        log.error('Все файлы не удалось конвертировать')
        messagebox.showerror(
            'Ошибка',
            'Не удалось конвертировать ни одного файла.\n\n'
            f'{details}\n\nПодробный лог:\n{LOG_PATH}',
        )
        return

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_zip = get_downloads_dir() / f'images_to_jpg_{timestamp}.zip'
    try:
        make_zip(jpg_files, output_zip)
    except Exception as e:  # noqa: BLE001
        log.exception('Не удалось создать ZIP')
        messagebox.showerror('Ошибка', f'Не удалось создать ZIP:\n{e}')
        _cleanup_tmp(jpg_files)
        return

    _cleanup_tmp(jpg_files)

    if errors:
        preview = '\n'.join(errors[:15])
        if len(errors) > 15:
            preview += f'\n… и ещё {len(errors) - 15}'
        messagebox.showwarning(
            'Готово, но с предупреждениями',
            f'Конвертировано: {len(jpg_files)}\n'
            f'Пропущено: {len(errors)}\n\n{preview}',
        )

    log.info('Готово: %s (%d файлов)', output_zip, len(jpg_files))
    open_path(output_zip)


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001
        log.exception('Необработанная ошибка')
        try:
            messagebox.showerror(
                'Критическая ошибка',
                f'Подробности в логе:\n{LOG_PATH}',
            )
        except Exception:  # noqa: BLE001
            pass
        raise