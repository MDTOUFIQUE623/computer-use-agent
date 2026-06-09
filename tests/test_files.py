import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from src.tools.files import FileTools
from pathlib import Path

ft = FileTools()

# Use a safe test folder on your desktop
# make sure to put the path of the desktop correctly as in some cases its just desktop and there is no onedrive in between
TEST_DIR = Path.home() /"OneDrive" /"Desktop" / "agent_test" # it can be this too - TEST_DIR = Path.home() / "Desktop" / "agent_test"
TEST_DIR.mkdir(exist_ok=True)

# 1. Create a test file
test_file = TEST_DIR / "hello.txt"
test_file.write_text("test content")
print("Test file created")

# 2. Create a subfolder
result = ft.create_folder(str(TEST_DIR / "subfolder"))
print("Create folder:", result.message)

# 3. Copy the file
result = ft.copy_file(str(test_file), str(TEST_DIR / "subfolder"))
print("Copy:", result.message)

# 4. Rename it
result = ft.rename_file(str(test_file), "hello_renamed.txt")
print("Rename:", result.message)

# 5. List files
result = ft.list_files(str(TEST_DIR), recursive=True)
print("List:", result.message, result.data["files"])

# 6. Find files
result = ft.find_files(str(TEST_DIR), "*.txt")
print("Find:", result.message, result.data["matches"])

# 7. Get info
result = ft.get_file_info(str(TEST_DIR / "subfolder" / "hello.txt"))
print("Info:", result.data)

# 8. Organize by type — dry run first
extra = TEST_DIR / "photo.jpg"
extra.write_bytes(b"fake jpg")
result = ft.organize_by_type(str(TEST_DIR), dry_run=True)
print("Organize dry run:", result.message, result.data["moved"])

print("\nAll file tests passed")