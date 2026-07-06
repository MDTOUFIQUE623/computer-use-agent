import sys
from pathlib import Path
 
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
 
from dotenv import load_dotenv
load_dotenv()
 
from src.tools.spotify_auth import SpotifyAuth
from src.config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI
 
 
def main() -> int:
    print(f"\n{'='*60}")
    print("  Spotify Authorization — one-time setup")
    print(f"{'='*60}\n")
 
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("  ✗  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set in .env")
        print("     Add them, then run this script again.")
        return 1
 
    print(f"  Redirect URI: {SPOTIFY_REDIRECT_URI}")
    print("  Make sure this exact URI is registered in your Spotify")
    print("  Developer Dashboard app settings, or the exchange will fail.\n")
 
    auth  = SpotifyAuth()
    token = auth.get_valid_token()
 
    if token:
        print(f"\n  ✓  Authorization successful.")
        print(f"     Cached at: {auth._cache_path}")
        print("     The agent can now use Spotify playback control.")
        return 0
 
    print("\n  ✗  Authorization did not complete. See messages above.")
    return 1
 
 
if __name__ == "__main__":
    sys.exit(main())
 