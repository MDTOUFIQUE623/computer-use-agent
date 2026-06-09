"""
BrowserTools YouTube automation test.
Searches for 'ishowspeed fifa 2026', plays the first result,
skips any ad, sets highest quality, goes fullscreen, plays to end.
"""

import sys
import os
import time

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from src.tools.browser import BrowserTools

with BrowserTools() as bt:

    # ----------------------------------------------------
    # 1. Open YouTube
    # ----------------------------------------------------

    result = bt.navigate("https://www.youtube.com")
    print("Navigate:", result.message)
    assert result.success

    # ----------------------------------------------------
    # 2. Wait for search box and fill it
    # ----------------------------------------------------

    assert bt.page.locator("input[name='search_query']").count() > 0

    bt.page.locator("input[name='search_query']").fill("ishowspeed fifa 2026")
    print("Typed search query")

    # ----------------------------------------------------
    # 3. Submit search and wait for results
    # ----------------------------------------------------

    bt.page.locator("input[name='search_query']").press("Enter")
    print("Submitted search")

    bt.page.wait_for_load_state("networkidle")
    time.sleep(2)

    bt.page.locator("ytd-video-renderer").first.wait_for()
    print("Results loaded")

    # ----------------------------------------------------
    # 4. Click first result
    # ----------------------------------------------------

    bt.page.locator("ytd-video-renderer").first.click()
    print("Clicked first video")

    bt.page.wait_for_load_state("networkidle")
    time.sleep(3)

    # ----------------------------------------------------
    # 5. Skip ad if present
    #    YouTube shows a skip button after ~5 seconds.
    #    We also handle the "Skip Ads" (plural) variant.
    # ----------------------------------------------------

    print("\nChecking for ads...")

    # Ad containers YouTube uses
    ad_selectors = [
        ".ytp-ad-skip-button",          # "Skip Ad"  button
        ".ytp-ad-skip-button-modern",   # newer skip button style
        ".ytp-skip-ad-button",          # alternate class
    ]

    # Poll for up to 15 s — the skip button only appears after ~5 s of ad
    for _ in range(15):
        for sel in ad_selectors:
            skip_btn = bt.page.locator(sel)
            if skip_btn.count() > 0 and skip_btn.first.is_visible():
                skip_btn.first.click()
                print("Ad skipped!")
                time.sleep(1)
                break
        else:
            # No visible skip button yet — keep waiting
            time.sleep(1)
            continue
        break   # exited inner loop via break → ad was skipped
    else:
        print("No skippable ad detected (or ad already finished)")

    # Extra safety: if a non-skippable short ad is still playing, wait it out
    # by checking whether the ad badge is still visible (max 30 s)
    ad_badge = bt.page.locator(".ytp-ad-simple-ad-badge, .ytp-ad-preview-container")
    waited = 0
    while ad_badge.count() > 0 and ad_badge.first.is_visible() and waited < 30:
        print(f"Non-skippable ad still running… ({waited}s)")
        time.sleep(2)
        waited += 2

    # ----------------------------------------------------
    # 6. Set highest available video quality
    #    Flow: Settings gear → Quality → pick top option
    # ----------------------------------------------------

    print("\nSetting video quality to highest…")

    # Open the settings menu (gear icon)
    bt.page.locator(".ytp-settings-button").click()
    time.sleep(1)

    # Click the "Quality" menu item
    quality_option = bt.page.locator(".ytp-menuitem-label", has_text="Quality")
    quality_option.first.wait_for(timeout=5_000)
    quality_option.first.click()
    time.sleep(0.8)

    # The quality sub-menu lists resolutions; the highest is always first
    # (YouTube orders them descending: 1080p, 720p, …)
    quality_items = bt.page.locator(".ytp-menuitem-label")
    quality_items.first.wait_for(timeout=5_000)

    # Grab all visible quality labels and click the first one (highest)
    all_labels = quality_items.all_text_contents()
    print(f"Available qualities: {all_labels}")
    quality_items.first.click()
    print(f"Selected quality: {all_labels[0] if all_labels else 'unknown'}")
    time.sleep(1)

    # ----------------------------------------------------
    # 7. Enter fullscreen
    # ----------------------------------------------------

    print("\nEntering fullscreen…")
    bt.page.locator(".ytp-fullscreen-button").click()
    time.sleep(2)
    print("Fullscreen active")

    # ----------------------------------------------------
    # 8. Verify video player and that playback has started
    # ----------------------------------------------------

    assert bt.page.locator("video").count() > 0, "Video element not found!"
    print("Video player confirmed")

    # Make sure the video is actually playing (currentTime advances)
    t0 = bt.page.eval_on_selector("video", "v => v.currentTime")
    time.sleep(3)
    t1 = bt.page.eval_on_selector("video", "v => v.currentTime")
    assert t1 > t0, f"Video does not appear to be playing (t0={t0}, t1={t1})"
    print(f"Playback confirmed (currentTime {t0:.1f}s → {t1:.1f}s)")

    # ----------------------------------------------------
    # 9. Print video info
    # ----------------------------------------------------

    result = bt.get_current_url()
    print(f"\nVideo URL : {result.data['current_url']}")
    print(f"Video title: {bt.page.title()}")

    # ----------------------------------------------------
    # 10. Play until the video ends
    #     Poll video.ended every 5 s; bail out after 3 h max.
    # ----------------------------------------------------

    print("\nWaiting for video to finish…")

    MAX_WAIT = 3 * 60 * 60   # 3-hour safety ceiling
    poll     = 5             # check every 5 seconds
    elapsed  = 0

    while elapsed < MAX_WAIT:
        ended   = bt.page.eval_on_selector("video", "v => v.ended")
        paused  = bt.page.eval_on_selector("video", "v => v.paused")
        current = bt.page.eval_on_selector("video", "v => v.currentTime")
        dur_raw = bt.page.eval_on_selector("video", "v => v.duration")

        duration = dur_raw if dur_raw and dur_raw == dur_raw else 0   # NaN guard

        if ended:
            print(f"\nVideo ended at {current:.0f}s — done!")
            break

        # If paused unexpectedly (e.g. buffering stall, autoplay policy), resume
        if paused and not ended:
            bt.page.eval_on_selector("video", "v => v.play()")
            print("Video was paused — resumed playback")

        pct = f"{current/duration*100:.1f}%" if duration else "?"
        print(f"  Playing… {current:.0f}s / {duration:.0f}s ({pct})", end="\r")

        time.sleep(poll)
        elapsed += poll

    else:
        print("\nMax wait time reached — exiting")

    # ----------------------------------------------------
    # 11. Done
    # ----------------------------------------------------

    print("\nYouTube automation test complete ✓")