# TheCollector

A Telegram Bot that downloads videos using yt-dlp and sends them back to the user.

Usage:
1. Install necessary libraries:
   pip install python-telegram-bot yt-dlp
2. Get a Bot Token from BotFather on Telegram.
3. Replace "YOUR_TELEGRAM_BOT_TOKEN" with your actual token.
4. Run the script: python your_script_name.py
5. Send a video URL to your bot on Telegram.

Notes:
- Telegram has a file size limit for bots sending files (usually 50MB).
  This bot attempts to download a suitable format but might fail for very large videos.
- Ensure ffmpeg is installed if yt-dlp requires it for merging formats or conversions.
  (yt-dlp often bundles it or can use a system-wide ffmpeg).