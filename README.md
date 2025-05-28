---
title: The Collector
emoji: 🔥
colorFrom: gray
colorTo: red
sdk: gradio
sdkVersion: "5.31.0"
app_file: app.py
pinned: true
license: mit
---

# TheCollector

A Telegram Bot that downloads videos or video segments using yt-dlp.

Usage:
1. Install necessary libraries:
   pip install python-telegram-bot yt-dlp
2. Get a Bot Token from BotFather on Telegram.
3. Replace "YOUR_TELEGRAM_BOT_TOKEN" with your actual token.
4. Run the script: python your_script_name.py
5. Send commands to your bot on Telegram:
   /download <video_url>
   /download <video_url> [START_TIME]
   /download <video_url> [START_TIME] [END_TIME]
   (Time format: HH:MM:SS or MM:SS)

Notes:
- Telegram has a file size limit for bots sending files (usually 50MB).
  This bot attempts to download a suitable format but might fail for very large videos.
- Ensure ffmpeg is installed if yt-dlp requires it for merging formats or conversions.
  (yt-dlp often bundles it or can use a system-wide ffmpeg).

Tasks:
- Deployment 
  - Set up docker first.
- Switch to using a subprocess