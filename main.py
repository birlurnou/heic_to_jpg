import os
import sys
import subprocess
import zipfile
import tempfile
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image
from pillow_heif import register_heif_opener


register_heif_opener()

def get_downloads_dir() -> Path:
    downloads = Path.home() / 'Downloads'
    if downloads.exists():
        return downloads
    return Path.cwd()


def open_file(path: Path) -> None:
    try:
        if sys.platform.startswith('win'):
            os.startfile(str(path))
        elif sys.platform == 'darwin':
            subprocess.run(['open', str(path)], check=False)
        else:
            subprocess.run(['xdg-open', str(path)], check=False)
    except Exception as e:
        pass


def convert_heic_files(heic_paths: list[Path], quality: int = 90) -> tuple[list[Path], list[str]]:
    tmp_dir = Path(tempfile.mkdtemp(prefix='heic2jpg_'))
    jpg_files: list[Path] = []
    errors: list[str] = []

    for heic in heic_paths:
        try:
            image = Image.open(heic)
            if image.mode in ('RGBA', 'P', 'LA'):
                image = image.convert('RGB')
            jpg_path = tmp_dir / (heic.stem + '.jpg')
            image.save(jpg_path, 'JPEG', quality=quality, optimize=True)
            jpg_files.append(jpg_path)
        except Exception as e:
            errors.append(f'{heic.name}: {e}')

    return jpg_files, errors


def make_zip(jpg_files: list[Path], output_zip: Path) -> None:
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for jpg in jpg_files:
            zf.write(jpg, arcname=jpg.name)


def main() -> None:
    root = tk.Tk()
    root.withdraw()

    # 1. выбор файлов через проводник
    selected = filedialog.askopenfilenames(
        title='Выберите HEIC-файлы',
        filetypes=[
            ('HEIC изображения', '*.heic *.HEIF *.heif'),
            ('Все файлы', '*.*'),
        ],
    )

    if not selected:
        return

    heic_paths = [Path(p) for p in selected]

    # 2. конвертация
    jpg_files, errors = convert_heic_files(heic_paths, quality=90)

    if not jpg_files:
        messagebox.showerror(
            'Ошибка',
            'Не удалось конвертировать ни одного файла.\n\n' + '\n'.join(errors),
        )
        return

    # 3. ZIP в Загрузки
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_zip = get_downloads_dir() / f'heic_to_jpg_{timestamp}.zip'
    make_zip(jpg_files, output_zip)

    # 4. чистим временные JPG
    for jpg in jpg_files:
        try:
            jpg.unlink()
        except OSError:
            pass
    try:
        jpg_files[0].parent.rmdir()
    except OSError:
        pass

    # 5. автоматически открываем ZIP
    open_file(output_zip)


if __name__ == '__main__':
    main()