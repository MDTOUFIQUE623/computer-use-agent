"""
Tests for AppTools.
Tests Spotify (media keys), SystemTools (clipboard, volume, processes).
Notion tests are skipped if API key not configured.
"""

import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
load_dotenv()

from src.tools.apps import SpotifyTools, NotionTools, SystemTools
import time

spotify = SpotifyTools()
system  = SystemTools()
notion  = NotionTools()

# -----------------------------------------------------------------------
# SystemTools tests — always run, no dependencies
# -----------------------------------------------------------------------

# # 1. Clipboard
# result = system.copy_to_clipboard("Hello from agent test")
# print("Copy to clipboard:", result.message)
# assert result.success

# result = system.get_clipboard()
# print("Get clipboard:", result.message)
# print("  Content:", result.data["content"])
# assert result.success
# assert result.data["content"] == "Hello from agent test"

# # 2. Process check
# result = system.is_process_running("python")
# print("\nPython process running:", result.message)
# assert result.success  # we're running Python right now

# result = system.is_process_running("definitely_not_a_real_process_xyz")
# print("Fake process:", result.message)
# assert not result.success

# # 3. Get running processes filtered
# result = system.get_running_processes(filter_name="python")
# print("\nPython processes:", result.message)
# assert result.success
# assert len(result.data["processes"]) > 0

# # 4. Wait
# result = system.wait_seconds(0.5)
# print("\nWait 0.5s:", result.message)
# assert result.success

# -----------------------------------------------------------------------
# Spotify tests — only if Spotify is running
# -----------------------------------------------------------------------

# In test_apps.py replace the Spotify state test with:

print("\n--- Spotify State Tests ---")

# Test with song playing
result = spotify.get_now_playing_ocr()
print("get_now_playing_ocr:", result.message)

if result.success:
    print("  Is playing:", result.data["is_playing"])
    print("  Track:",      result.data["track"])
    if result.data.get("lines"):
        print("  Bar lines:")
        for line in result.data["lines"][:4]:
            print(f"    {line!r}")
else:
    print("  Error:", result.error)
# -----------------------------------------------------------------------
# Notion tests — only if API key configured
# -----------------------------------------------------------------------

# if notion._is_configured():
#     print("\nNotion API configured — running Notion tests")

#     # Search for pages
#     result = notion.search_pages("test")
#     print("Search pages:", result.message)
#     if result.success:
#         pages = result.data["pages"]
#         print(f"  Found {len(pages)} page(s)")
#         for p in pages[:3]:
#             print(f"  - {p['title']} ({p['page_id']})")

# else:
#     print("\nNotion API key not configured — skipping Notion tests")
#     print("Add NOTION_API_KEY to .env to enable Notion integration")

# print("\nAll apps tests passed")


#  ----------- different test_------------------

# import sys, os
# sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

# from src.tools.apps import SpotifyTools
# spotify = SpotifyTools()

# # # Both should return identical results
# # r1 = spotify.get_window_title()
# # r2 = spotify.get_current_state()

# # print("get_window_title:", r1.message)
# # print("get_current_state:", r2.message)
# # print("Same result:", r1.message == r2.message)

# # # Only access data if successful
# # if r2.success:
# #     print("Is playing:", r2.data.get("is_playing"))
# #     print("Track:", r2.data.get("track"))
# # else:
# #     print("Spotify not open — open Spotify and run again")

# # Replace the test block with:
# result = spotify.get_now_playing_ocr()
# print("get_now_playing_ocr:", result.message)
# if result.success and result.data.get("lines"):
#     print("  Bar text lines:")
#     for line in result.data["lines"][:5]:
#         print(f"    {line}")

