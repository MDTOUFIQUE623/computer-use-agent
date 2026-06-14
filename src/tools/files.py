import os
import shutil
import time
import logging
from pathlib import Path
from typing import Optional

from src.models import ToolResult

log = logging.getLogger(__name__)

class FileTools:
    """
    All file/folder operations the agent can perform.
    
    Usage:
        ft = FileTools()
        result = ft.move_file("C:/Downloads/report.pdf", "C:/Documents/")
        if result.success:
            print(result.message)
    """

    # -----------------------------------------------------------------------
    # Move
    # -----------------------------------------------------------------------

    def move_file(self, source: str, destination: str) -> ToolResult:
        """
        Move a file or folder to destination.
        destination can be a folder path (file keeps its name)
        or a full path including new filename.
        """
        start = time.monotonic()
        try:
            src  = Path(source).resolve()
            dst  = Path(destination).resolve()

            if not src.exists():
                return ToolResult(
                    success=False,
                    message=f"Source not found: {source}",
                    error="FileNotFoundError"
                )

            # If destination is a directory, keep original filename
            if dst.is_dir():
                dst = dst / src.name

            # Create parent dirs if they don't exist
            dst.parent.mkdir(parents=True, exist_ok=True)

            shutil.move(str(src), str(dst))

            return ToolResult(
                success=True,
                message=f"Moved '{src.name}' to '{dst.parent}'",
                data={"destination": str(dst)},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("move_file failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to move '{source}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Copy
    # -----------------------------------------------------------------------

    def copy_file(self, source: str, destination: str) -> ToolResult:
        """
        Copy a file to destination.
        If destination is a directory, file keeps its name.
        """
        start = time.monotonic()
        try:
            src = Path(source).resolve()
            dst = Path(destination).resolve()

            if not src.exists():
                return ToolResult(
                    success=False,
                    message=f"Source not found: {source}",
                    error="FileNotFoundError"
                )

            if dst.is_dir():
                dst = dst / src.name

            dst.parent.mkdir(parents=True, exist_ok=True)

            if src.is_dir():
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))

            return ToolResult(
                success=True,
                message=f"Copied '{src.name}' to '{dst.parent}'",
                data={"destination": str(dst)},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("copy_file failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to copy '{source}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Rename
    # -----------------------------------------------------------------------

    def rename_file(self, source: str, new_name: str) -> ToolResult:
        """
        Rename a file or folder.
        new_name is just the name part, not a full path.
        The file stays in the same directory.
        """
        start = time.monotonic()
        try:
            src      = Path(source).resolve()
            new_path = src.parent / new_name

            if not src.exists():
                return ToolResult(
                    success=False,
                    message=f"Source not found: {source}",
                    error="FileNotFoundError"
                )

            if new_path.exists():
                return ToolResult(
                    success=False,
                    message=f"A file named '{new_name}' already exists here",
                    error="FileExistsError"
                )

            src.rename(new_path)

            return ToolResult(
                success=True,
                message=f"Renamed '{src.name}' to '{new_name}'",
                data={"new_path": str(new_path)},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("rename_file failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to rename '{source}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Delete
    # -----------------------------------------------------------------------

    def delete_file(
        self,
        source: str,
        confirm: bool = False
    ) -> ToolResult:
        """
        Delete a file or folder.
        confirm=True is required for non-empty folders as a safety check.
        Single files are always deleted without confirm.
        """
        start = time.monotonic()
        try:
            src = Path(source).resolve()

            if not src.exists():
                return ToolResult(
                    success=False,
                    message=f"Not found: {source}",
                    error="FileNotFoundError"
                )

            # Non-empty folder needs explicit confirm
            if src.is_dir():
                contents = list(src.iterdir())
                if contents and not confirm:
                    return ToolResult(
                        success=False,
                        message=(
                            f"Folder '{src.name}' is not empty "
                            f"({len(contents)} items). "
                            f"Pass confirm=True to delete anyway."
                        ),
                        error="SafetyCheck"
                    )
                shutil.rmtree(str(src))
            else:
                src.unlink()

            return ToolResult(
                success=True,
                message=f"Deleted '{src.name}'",
                data={"path": str(src)},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("delete_file failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to delete '{source}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Create folder
    # -----------------------------------------------------------------------

    def create_folder(self, path: str) -> ToolResult:
        """
        Create a folder and any missing parent folders.
        Does not fail if folder already exists.
        """
        start = time.monotonic()
        try:
            folder = Path(path).resolve()
            folder.mkdir(parents=True, exist_ok=True)

            return ToolResult(
                success=True,
                message=f"Folder ready: '{folder}'",
                data={"path": str(folder)},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("create_folder failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to create folder '{path}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # List files
    # -----------------------------------------------------------------------

    def list_files(
        self,
        folder: str,
        extension: Optional[str] = None,
        recursive: bool = False
    ) -> ToolResult:
        """
        List files in a folder.
        extension: filter by extension e.g. ".pdf", ".png"
        recursive: include subdirectories
        """
        start = time.monotonic()
        try:
            folder_path = Path(folder).resolve()

            if not folder_path.is_dir():
                return ToolResult(
                    success=False,
                    message=f"Not a directory: {folder}",
                    error="NotADirectoryError"
                )

            # Choose glob pattern
            pattern = "**/*" if recursive else "*"
            all_items = list(folder_path.glob(pattern))

            # Filter to files only
            files = [p for p in all_items if p.is_file()]

            # Filter by extension if given
            if extension:
                ext = extension.lower().lstrip(".")
                files = [f for f in files if f.suffix.lower().lstrip(".") == ext]

            file_list = [str(f) for f in sorted(files)]

            return ToolResult(
                success=True,
                message=f"Found {len(file_list)} file(s) in '{folder_path.name}'",
                data={"files": file_list, "count": len(file_list)},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("list_files failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to list files in '{folder}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Find files
    # -----------------------------------------------------------------------

    def find_files(
        self,
        folder: str,
        pattern: str,
        recursive: bool = True
    ) -> ToolResult:
        """
        Find files matching a glob pattern.
        pattern examples: "*.pdf", "report*", "2024*budget*.xlsx"
        Searches recursively by default.
        """
        start = time.monotonic()
        try:
            folder_path = Path(folder).resolve()

            if not folder_path.is_dir():
                return ToolResult(
                    success=False,
                    message=f"Not a directory: {folder}",
                    error="NotADirectoryError"
                )

            glob_pattern = f"**/{pattern}" if recursive else pattern
            matches = [
                str(p) for p in folder_path.glob(glob_pattern)
                if p.is_file()
            ]

            return ToolResult(
                success=True,
                message=f"Found {len(matches)} match(es) for '{pattern}'",
                data={"matches": matches, "count": len(matches)},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("find_files failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to search in '{folder}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Organize by type
    # -----------------------------------------------------------------------

    def organize_by_type(
        self,
        folder: str,
        dry_run: bool = False
    ) -> ToolResult:
        """
        Sort files in a folder into subfolders by extension.
        dry_run=True shows what WOULD happen without moving anything.

        Extension → subfolder mapping:
          Images    : .jpg .jpeg .png .gif .bmp .webp .svg
          Documents : .pdf .doc .docx .txt .md .xlsx .xls .pptx .csv
          Videos    : .mp4 .mov .avi .mkv .wmv
          Audio     : .mp3 .wav .flac .aac .ogg
          Code      : .py .js .ts .html .css .json .yaml .toml
          Archives  : .zip .rar .7z .tar .gz
          Others    : everything else
        """
        start = time.monotonic()

        TYPE_MAP = {
            "Images":    {".jpg",".jpeg",".png",".gif",".bmp",".webp",".svg"},
            "Documents": {".pdf",".doc",".docx",".txt",".md",
                          ".xlsx",".xls",".pptx",".csv"},
            "Videos":    {".mp4",".mov",".avi",".mkv",".wmv"},
            "Audio":     {".mp3",".wav",".flac",".aac",".ogg"},
            "Code":      {".py",".js",".ts",".html",".css",
                          ".json",".yaml",".toml"},
            "Archives":  {".zip",".rar",".7z",".tar",".gz"},
        }

        def _get_category(ext: str) -> str:
            ext_lower = ext.lower()
            for category, extensions in TYPE_MAP.items():
                if ext_lower in extensions:
                    return category
            return "Others"

        try:
            folder_path = Path(folder).resolve()

            if not folder_path.is_dir():
                return ToolResult(
                    success=False,
                    message=f"Not a directory: {folder}",
                    error="NotADirectoryError"
                )

            # Only look at direct children that are files
            files = [f for f in folder_path.iterdir() if f.is_file()]

            if not files:
                return ToolResult(
                    success=True,
                    message="No files to organize",
                    data={"moved": {}}
                )

            moved: dict[str, str] = {}

            for file in files:
                category   = _get_category(file.suffix)
                target_dir = folder_path / category
                target     = target_dir / file.name

                moved[str(file)] = str(target)

                if not dry_run:
                    target_dir.mkdir(exist_ok=True)
                    shutil.move(str(file), str(target))

            action_word = "Would move" if dry_run else "Moved"
            return ToolResult(
                success=True,
                message=(
                    f"{action_word} {len(moved)} file(s) into "
                    f"category subfolders"
                    + (" (dry run)" if dry_run else "")
                ),
                data={"moved": moved, "dry_run": dry_run},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("organize_by_type failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to organize '{folder}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Get file info
    # -----------------------------------------------------------------------

    def get_file_info(self, path: str) -> ToolResult:
        """
        Return metadata about a file or folder.
        """
        start = time.monotonic()
        try:
            p = Path(path).resolve()

            if not p.exists():
                return ToolResult(
                    success=False,
                    message=f"Not found: {path}",
                    error="FileNotFoundError"
                )

            stat = p.stat()
            info = {
                "name":        p.name,
                "path":        str(p),
                "type":        "folder" if p.is_dir() else "file",
                "size_bytes":  stat.st_size,
                "size_kb":     round(stat.st_size / 1024, 2),
                "extension":   p.suffix.lower() if p.is_file() else None,
                "modified":    stat.st_mtime,
                "exists":      True,
            }

            return ToolResult(
                success=True,
                message=f"Got info for '{p.name}'",
                data=info,
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("get_file_info failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to get info for '{path}'",
                error=str(e),
                duration_ms=_ms(start)
            )
        
    def write_file(
        self,
        path: str,
        content: str,
        append: bool = False,
    ) -> ToolResult:
        """
        Write text content to a file.
        Creates the file if it doesn't exist.
        append=True adds to existing content instead of overwriting.
        """
        start = time.monotonic()
        try:
            p = Path(path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            with open(p, mode, encoding="utf-8") as f:
                f.write(content)

            size = p.stat().st_size
            return ToolResult(
                success=True,
                message=f"{'Appended to' if append else 'Wrote'} '{p.name}' ({size} bytes)",
                data={
                    "path":    str(p),
                    "size":    size,
                    "append":  append,
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("write_file failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to write '{path}'",
                error=str(e),
                duration_ms=_ms(start)
            )


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _ms(start: float) -> int:
    """Elapsed milliseconds since start."""
    return int((time.monotonic() - start) * 1000)