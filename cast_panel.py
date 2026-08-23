#!/usr/bin/env python3
import time
import sys
import os
import logging
from catt.discovery import get_cast
from catt.controllers import DashCastController, get_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("cast_panel")

DEVICE_IP = os.getenv("CAST_DEVICE_IP", "192.168.1.118")
SITE_URL = os.getenv("CAST_SITE_URL", "http://192.168.1.110:5050/")
FORCE_CAST = "--force" in sys.argv

def main():
    try:
        logger.info(f"Connecting to Google Cast device at {DEVICE_IP}...")
        cast = get_cast(DEVICE_IP)
        if not cast:
            logger.error("Failed to connect to Cast device.")
            sys.exit(1)
            
        app_name = cast.app_display_name
        app_id = cast.app_id
        is_muted = cast.status.volume_muted
        current_volume = cast.status.volume_level
        
        logger.info(f"Connected to '{cast.name}'. Current App: '{app_name}' (ID: {app_id}), Volume: {int(current_volume*100)}%, Muted: {is_muted}")

        # If DashCast is already running and not forced, don't recast
        if not FORCE_CAST and app_name == "DashCast":
            logger.info("DashCast is already running. No need to recast.")
            return

        # If user is actively playing media (Spotify, YouTube, Radio, etc.), don't interrupt
        media_apps = ["Spotify", "YouTube", "YouTube Music", "Netflix", "TuneIn Free", "Radio"]
        if not FORCE_CAST and (app_name in media_apps or (hasattr(cast, "media_controller") and cast.media_controller.status.player_state == "PLAYING")):
            logger.info(f"User is actively playing media ({app_name}). Skipping recast to avoid interruption.")
            return

        logger.info(f"Starting silent cast of {SITE_URL}...")
        
        # 1. Silence device before launching app to prevent Google Cast chime
        cast.set_volume_muted(True)
        cast.set_volume(0.0)
        time.sleep(0.4)

        # 2. Launch DashCast with the URL
        dashcast_app = get_app("dashcast", cast.cast_type)
        controller = DashCastController(cast, dashcast_app, prep="app")
        controller.load_url(SITE_URL)
        logger.info("DashCast launched. Waiting for connection tone window to pass...")

        # 3. Wait for Google Cast connection tone window to finish silently
        time.sleep(3.5)

        # 4. Restore previous volume and mute state
        cast.set_volume(current_volume)
        cast.set_volume_muted(is_muted)
        logger.info(f"Volume restored to {int(current_volume*100)}% (Muted: {is_muted}). Silent cast complete!")

    except Exception as e:
        logger.error(f"Error during cast: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
