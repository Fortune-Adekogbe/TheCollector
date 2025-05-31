#!/usr/bin/env python
# pylint: disable=unused-argument, wrong-import-position
# This program is dedicated to the public domain under the MIT license.

import logging
import os
import re
import asyncio
import yt_dlp
from telegram import Update, InputFile
from telegram import BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from dotenv import load_dotenv

load_dotenv()  # take environment variables

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("YOUR_TELEGRAM_BOT_TOKEN")  # Replace with your bot token
DOWNLOAD_PATH = "video_downloads/"  # Folder to store downloaded videos temporarily
MAX_FILE_SIZE_MB = 49  # Telegram's typical bot upload limit is 50MB, stay slightly under

# --- !! IMPORTANT !! ---
# Paths to your Netscape format cookies files.
YOUTUBE_COOKIES_FILE="cookies/youtube.txt"
INSTAGRAM_COOKIES_FILE="cookies/instagram.txt"

# --- Helper Functions ---
def ensure_download_path_exists():
    """Creates the download directory if it doesn't exist."""
    if not os.path.exists(DOWNLOAD_PATH):
        try:
            os.makedirs(DOWNLOAD_PATH)
            logger.info(f"Created download directory: {DOWNLOAD_PATH}")
        except OSError as e:
            logger.error(f"Error creating download directory {DOWNLOAD_PATH}: {e}")
            # Depending on the desired behavior, you might want to exit or raise the exception
            # For this example, we'll log and continue, but downloads will likely fail.


async def send_typing_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a typing action to indicate the bot is working."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='upload_video')

def is_time_like(text: str) -> bool:
    """Basic check if a string looks like a time argument (contains ':' or is all digits)."""
    if not text:
        return False
    return ':' in text or text.isdigit()

def is_youtube_url(url: str) -> bool:
    """Checks if the URL is a YouTube URL."""
    youtube_regex = (
        r'(https?://)?(www\.)?'
        '(youtube|youtu|youtube-nocookie)\.(com|be)/'
        '(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})')
    return bool(re.match(youtube_regex, url))

def is_instagram_url(url: str) -> bool:
    """Checks if the URL is an Instagram URL."""
    instagram_regex = r'(https?://)?(www\.)?instagram\.com/(p|reel|tv)/([^/?#&]+)'
    return bool(re.match(instagram_regex, url))

# --- Bot Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    welcome_message = (
       f"👋 Hello {user.mention_html()}!\n\n"
        "I'm your video downloading bot.\n"
        "Use `/download <URL> [START_TIME] [END_TIME]` to fetch a video or a segment.\n"
        "Times are optional (e.g., `MM:SS` or `HH:MM:SS`).\n\n"
        "Example (full video): `/download <your_video_url>`\n"
        "Example (segment): `/download <your_video_url> 00:10 00:50`\n\n"
        "Type /help for more detailed information."
    )
    await update.message.reply_html(welcome_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a help message when the /help command is issued."""
    help_text = (
        "ℹ️ **How to use me:**\n"
        "Use the `/download` command followed by a video URL.\n"
        "You can optionally specify a start and/or end time for the segment.\n\n"
        "**Formats:**\n"
        "1. `/download <VIDEO_URL>`\n"
        "   Downloads the full video.\n\n"
        "2. `/download <VIDEO_URL> <START_TIME>`\n"
        "   Downloads from `START_TIME` to the end of the video.\n"
        "   Example: `/download <url> 01:20` (starts at 1 min 20 secs)\n\n"
        "3. `/download <VIDEO_URL> <START_TIME> <END_TIME>`\n"
        "   Downloads the segment between `START_TIME` and `END_TIME`.\n"
        "   Example: `/download <url> 00:30 02:15`\n\n"
        "Time format can be `MM:SS` or `HH:MM:SS` (e.g., `1:23` or `00:01:23`).\n"
        "Use `0` or `00:00` for the beginning if specifying an end time only (e.g. `/download <url> 0 00:55`).\n\n"
        "**Supported Sites:**\n"
        "Most sites supported by `yt-dlp` (YouTube, Vimeo, Twitter, etc.).\n\n"
        "**File Size Limit:**\n"
        "Telegram bots can only send files up to ~50MB. I'll try to get a version under this. "
        "Segments are more likely to fit!"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def downloader(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> tuple:
    chat_id = update.effective_chat.id

    if not url.startswith(('http://', 'https://')):
        await update.message.reply_text("⚠️ That doesn't look like a valid URL. Please send a direct link to a video.")
        return

    processing_message = await update.message.reply_text("🔍 Got your link! Processing and trying to download the video...")
    await send_typing_action(update, context)

    downloaded_file_path = None  # To store the path of the downloaded file

    # Get the current event loop to schedule coroutines from the hook
    main_event_loop = asyncio.get_event_loop()

    try:
        # yt-dlp options
        # We aim for a good quality mp4 file under 50MB.
        # 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b' is a common format string.
        # We add filesize limits.
        # Note: yt-dlp's filesize_approx can sometimes be inaccurate.
        ydl_opts = {
            'format': f'bestvideo[ext=mp4][filesize<{MAX_FILE_SIZE_MB}M]+bestaudio[ext=m4a]/best[ext=mp4][filesize<{MAX_FILE_SIZE_MB}M]/best[filesize<{MAX_FILE_SIZE_MB}M]',
            'outtmpl': os.path.join(DOWNLOAD_PATH, '%(title).200B.%(ext)s'), # Limit title length to avoid overly long filenames
            'noplaylist': True, # Download only single video if playlist URL is given
            # 'quiet': True, # Suppress yt-dlp console output
            'merge_output_format': 'mp4', # Ensure output is mp4 if merging is needed
            'max_filesize': MAX_FILE_SIZE_MB * 1024 * 1024, # Alternative way to specify max filesize
            # 'postprocessors': [{
            #     'key': 'FFmpegVideoConvertor',
            #     'preferedformat': 'mp4',
            # }],
            # 'logger': logger, # Send yt-dlp logs to our logger
            'progress_hooks': [
                lambda d: asyncio.run_coroutine_threadsafe(
                    download_progress_hook(d, update, context, processing_message.message_id),
                    main_event_loop
                )
            ],
            # 'verbose': True
        }

        # Add cookies based on URL
        if is_youtube_url(url):
            print("here")
            if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
                ydl_opts['cookies'] = YOUTUBE_COOKIES_FILE
                logger.info(f"Using YouTube cookies file: {YOUTUBE_COOKIES_FILE}")
            else:
                logger.warning(f"YouTube URL detected, but YouTube cookies file not found or not configured: {YOUTUBE_COOKIES_FILE}")
        elif is_instagram_url(url):
            if INSTAGRAM_COOKIES_FILE and os.path.exists(INSTAGRAM_COOKIES_FILE):
                del ydl_opts['format'] # TEMP FIX FOR INSTAGRAM DOWNLOAD ISSUE.
                ydl_opts['cookies'] = INSTAGRAM_COOKIES_FILE
                logger.info(f"Using Instagram cookies file: {INSTAGRAM_COOKIES_FILE}")
            else:
                logger.warning(f"Instagram URL detected, but Instagram cookies file not found or not configured: {INSTAGRAM_COOKIES_FILE}")
                
        else: # Full video download
            if 'recodevideo' not in ydl_opts: # Only add if not already recoding (e.g. for segments)
                 ydl_opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
            logger.info(f"Full video download requested for {url}")
            # user_feedback_time_segment = " (full video)"
        
        # Use asyncio.to_thread to run blocking yt-dlp code in a separate thread
        # This prevents the bot from freezing during download.
        loop = asyncio.get_event_loop()

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # We need to run extract_info and download in a thread-safe way
            # First, get info to check file size if possible (though format selection handles much of this)
            try:
                # Using download=False first to inspect, then download=True
                # This is a bit more complex; for simplicity, we'll try direct download
                # with format selectors doing the heavy lifting for size.
                logger.info(f"Attempting to download: {url}")
                # The actual download happens here
                # result = await loop.run_in_executor(None, ydl.download, [url])

                # More robust way to get the filename:
                info_dict = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                
                # Try to find a suitable format based on filesize if extract_info provides it
                # This is an advanced step; ydl_opts['format'] usually handles it.
                # For now, we rely on the format selector.

                # Perform the download
                await loop.run_in_executor(None, lambda: ydl.download([url]))
                
                # Determine the filename
                # ydl.prepare_filename(info_dict) is usually reliable IF info_dict is from a non-download extract_info call
                # If download=True was used, we need to find the file.
                # A common way is to list files in DOWNLOAD_PATH if we expect only one.
                # For this example, we rely on the outtmpl and the hook to get the filename.
                # The `status: finished` hook will give us the final filename.
                # We'll retrieve it from context.chat_data if the hook sets it.

                downloaded_file_path = context.chat_data.pop(f'download_filename_{chat_id}', None)

                if not downloaded_file_path or not os.path.exists(downloaded_file_path):
                    # Fallback: try to find the latest mp4 file in the directory (less robust)
                    list_of_files = [os.path.join(DOWNLOAD_PATH, f) for f in os.listdir(DOWNLOAD_PATH) if f.endswith(".mp4")]
                    if list_of_files:
                        downloaded_file_path = max(list_of_files, key=os.path.getctime)
                        logger.info(f"Found downloaded file by fallback: {downloaded_file_path}")
                    else:
                        logger.error("Could not determine downloaded file path after download.")
                        await processing_message.edit_text("❌ Download seemed to complete, but I couldn't find the file. Please try again.")
                        return
                
                file_size = os.path.getsize(downloaded_file_path)
                logger.info(f"File downloaded: {downloaded_file_path}, Size: {file_size / (1024*1024):.2f} MB")


            except yt_dlp.utils.DownloadError as e:
                logger.error(f"yt-dlp DownloadError for URL {url}: {e}")
                error_message = f"❌ Failed to download video.\nError: `{str(e)}`"
                if "Unsupported URL" in str(e):
                    error_message = "❌ Sorry, this website or video URL is not supported."
                elif "Video unavailable" in str(e):
                    error_message = "❌ This video is unavailable or private."
                elif "Unable to extract" in str(e):
                     error_message = "❌ Could not extract video information. The link might be broken or unsupported."
                await processing_message.edit_text(error_message, parse_mode=ParseMode.MARKDOWN)
                return
            except Exception as e: # Catch other yt-dlp related errors
                logger.error(f"yt-dlp generic error for URL {url}: {e}")
                await processing_message.edit_text(f"❌ An error occurred during video processing: {str(e)}")
                return
        # if start_time_str and end_time_str: # Both start and end time
        #     logger.info(f"Segment (start-end): recode=mp4, download_sections, force_keyframes, pp_args for FFmpegVideoConvertor.")
        
        # elif start_time_str: # Only start time given
        #     logger.info(f"Segment (start-onwards): recode=mp4, download_sections, force_keyframes, pp_args with -ss for FFmpegVideoConvertor.")
        
        # else: # Full video download
        #     logger.info(f"Full video download requested for {url}")
        
    except Exception as e:
        logger.error(f"An overarching unexpected error occurred while downloading URL {url}: {e}", exc_info=True)
        await processing_message.edit_text("❌ An unexpected error occurred. Please try again later.")
    return downloaded_file_path, processing_message


# --- Main Video Processing Logic ---
async def download_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles messages containing video URLs."""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("⚠️ URL missing. Usage: /download <URL> [start] [end]"); return

    url = context.args[0]

    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a video URL after the /download command.\n"
            "Example: `/download https://www.youtube.com/watch?v=dQw4w9WgXcQ`", # Corrected example URL
            parse_mode=ParseMode.MARKDOWN
        )
        return

    url = context.args[0] # The first argument after /download

    # Parse optional start and end times
    start_time_str = None
    end_time_str = None

    if len(context.args) >= 2:
        potential_start_time = context.args[1]
        if is_time_like(potential_start_time): # Basic check
            start_time_str = potential_start_time
        else:
            await update.message.reply_text(f"⚠️ '{potential_start_time}' doesn't look like a valid start time (e.g., MM:SS). Downloading full video or stopping.")
            # Decide if you want to proceed with full download or stop. For now, we'll proceed assuming no valid time.
    
    if len(context.args) >= 3 and start_time_str: # Only look for end_time if start_time was plausible
        potential_end_time = context.args[2]
        if is_time_like(potential_end_time):
            end_time_str = potential_end_time
        else:
            await update.message.reply_text(f"⚠️ '{potential_end_time}' doesn't look like a valid end time. Will download from {start_time_str} to end if applicable.")

    downloaded_file_path, processing_message = await downloader(update, context, url)

    try:
        if downloaded_file_path and os.path.exists(downloaded_file_path):
            file_size = os.path.getsize(downloaded_file_path)

            if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                logger.warning(f"Video {downloaded_file_path} is too large: {file_size / (1024*1024):.2f} MB")
                await processing_message.edit_text(
                    f"⚠️ The downloaded video is too large ({file_size / (1024*1024):.2f} MB) "
                    f"for me to send via Telegram (max ~{MAX_FILE_SIZE_MB}MB). I tried to get a smaller version."
                )
                return # No cleanup here, user might want to access it if bot is self-hosted

            logger.info(f"PRE-SEND CHECK: Attempting to send video from path: '{downloaded_file_path}'")
            logger.info(f"PRE-SEND CHECK: Does file exist at this exact path? {os.path.exists(downloaded_file_path)}")
            logger.info(f"PRE-SEND CHECK: File size is {file_size / (1024*1024):.2f} MB")

            await processing_message.edit_text("✅ Download complete! Now uploading to Telegram...")
            await send_typing_action(update, context)

            try:
                with open(downloaded_file_path, 'rb') as video_file:
                    # For InputFile, you can pass a file path directly or a file-like object.
                    # Using a file path directly is often simpler.
                    # await context.bot.send_video(chat_id=chat_id, video=video_file, supports_streaming=True, caption=os.path.basename(downloaded_file_path))
                    logger.info("Attempting to send video using the file object directly...") # New log line
                    sent_message = await context.bot.send_video(
                        chat_id=chat_id,
                        video=video_file, # USE THIS INSTEAD
                        filename=os.path.basename(downloaded_file_path), # Good to add filename when sending file object
                        caption=f"🎬 Here's your video!\nOriginal URL: {url}",
                        supports_streaming=True,
                        read_timeout=180, # Increased timeout slightly for testing
                        write_timeout=180, # Increased timeout slightly for testing
                        connect_timeout=60
                    )
                await processing_message.delete() # Delete "Processing..." message
                logger.info(f"Video sent to chat_id {chat_id}: {downloaded_file_path}")

            except Exception as e: # Catch errors during Telegram upload
                logger.error(f"Error sending video to Telegram: {e}")
                await processing_message.edit_text(f"❌ Failed to upload video to Telegram: {str(e)}")
        else:
            if not context.chat_data.get(f'download_error_{chat_id}'): # If no specific download error was already sent by hook
                await processing_message.edit_text("❌ Download failed or no file was produced. Please check the URL or try again.")
            # Clear any error flag
            context.chat_data.pop(f'download_error_{chat_id}', None)


    except Exception as e:
        logger.error(f"An overarching unexpected error occurred for URL {url}: {e}", exc_info=True)
        await processing_message.edit_text("❌ An unexpected error occurred. Please try again later.")
    finally:
        # Clean up the downloaded file
        if downloaded_file_path and os.path.exists(downloaded_file_path):
            try:
                os.remove(downloaded_file_path)
                logger.info(f"Cleaned up downloaded file: {downloaded_file_path}")
            except OSError as e:
                logger.error(f"Error deleting file {downloaded_file_path}: {e}")
        # Clear any stored filename from chat_data
        context.chat_data.pop(f'download_filename_{chat_id}', None)
        context.chat_data.pop(f'download_error_{chat_id}', None)
        context.chat_data.pop(f'last_progress_msg_{chat_id}', None)


# --- yt-dlp Progress Hook ---
# Keep track of messages to edit for progress (to avoid spamming)
progress_message_ids = {} # chat_id: message_id

async def download_progress_hook(d, update: Update, context: ContextTypes.DEFAULT_TYPE, initial_message_id: int):
    """yt-dlp progress hook to update Telegram message."""
    chat_id = update.effective_chat.id
    
    if d['status'] == 'downloading':
        percent_str = d.get('_percent_str', 'N/A')
        # Remove ANSI codes if present (yt-dlp might use them)
        percent_str = percent_str.replace('\x1b[0;94m', '').replace('\x1b[0m', '')
        
        total_bytes_str = d.get('_total_bytes_str', 'N/A')
        speed_str = d.get('_speed_str', 'N/A')
        eta_str = d.get('_eta_str', 'N/A')

        # To avoid hitting Telegram rate limits, only update message periodically
        # This simple version updates on each hook call, which might be too frequent.
        # A better approach would involve a timer or updating every N percent.
        try:
            # Use the initial "Processing..." message to show progress
            current_progress_message = f"Downloading...\nProgress: {percent_str}\nSize: {total_bytes_str}\nSpeed: {speed_str}\nETA: {eta_str}"
            
            # Only edit if the message content has changed significantly
            last_message = context.chat_data.get(f'last_progress_msg_{chat_id}', "")
            if last_message != current_progress_message: # Basic check to avoid identical edits
                await context.bot.edit_message_text(
                    text=current_progress_message,
                    chat_id=chat_id,
                    message_id=initial_message_id
                )
                context.chat_data[f'last_progress_msg_{chat_id}'] = current_progress_message
        except Exception as e:
            # logger.warning(f"Could not edit progress message: {e}") # Can be noisy
            pass # Ignore if editing fails (e.g., message not found or too old)

    elif d['status'] == 'finished':
        logger.info(f"yt-dlp finished processing for chat {chat_id}. Filename: {d.get('filename') or d.get('info_dict', {}).get('_filename')}")
        # Store the final filename in chat_data to be picked up by the main handler
        # yt-dlp provides filename in different places depending on version/context
        final_filename = d.get('filename') # For when download=True
        if not final_filename and d.get('info_dict'): # For when download=False then True, or from info_dict
             final_filename = d['info_dict'].get('_filename')

        if final_filename:
            context.chat_data[f'download_filename_{chat_id}'] = final_filename
        
        try:
            await context.bot.edit_message_text(
                text="✅ Download finished by yt-dlp. Preparing to send...",
                chat_id=chat_id,
                message_id=initial_message_id
            )
        except Exception:
            pass # Ignore if editing fails
        # Clean up last progress message cache
        context.chat_data.pop(f'last_progress_msg_{chat_id}', None)

    elif d['status'] == 'error':
        logger.error(f"yt-dlp reported an error for chat {chat_id}.")
        context.chat_data[f'download_error_{chat_id}'] = True # Flag an error
        try:
            await context.bot.edit_message_text(
                text="❌ yt-dlp encountered an error during download.",
                chat_id=chat_id,
                message_id=initial_message_id
            )
        except Exception:
            pass # Ignore if editing fails

async def post_init(application: Application) -> None:
    """Sets the bot's commands after initialization."""
    commands = [
        BotCommand("start", "Starts the bot and shows a welcome message."),
        BotCommand("help", "Shows the help message with instructions."),
        BotCommand("download", "Downloads a video or segment. Usage: /download <URL> [start] [end]")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands have been set programmatically.")

# --- Main Bot Execution ---
def main() -> None:
    """Starts the bot."""
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.error("CRITICAL: Bot token is not set! Please replace 'YOUR_TELEGRAM_BOT_TOKEN' with your actual bot token.")
        return
    
    if YOUTUBE_COOKIES_FILE and not os.path.exists(YOUTUBE_COOKIES_FILE): # Check if configured AND not found
        logger.warning(f"YouTube cookies file configured but not found at '{YOUTUBE_COOKIES_FILE}'. YouTube downloads might fail.")
    elif YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
        logger.info(f"YouTube cookies file found at '{YOUTUBE_COOKIES_FILE}'. Will be used for YouTube URLs.")

    if INSTAGRAM_COOKIES_FILE and not os.path.exists(INSTAGRAM_COOKIES_FILE): # Check if configured AND not found
        logger.warning(f"Instagram cookies file configured but not found at '{INSTAGRAM_COOKIES_FILE}'. Instagram downloads might fail.")
    elif INSTAGRAM_COOKIES_FILE and os.path.exists(INSTAGRAM_COOKIES_FILE):
        logger.info(f"Instagram cookies file found at '{INSTAGRAM_COOKIES_FILE}'. Will be used for Instagram URLs.")

    ensure_download_path_exists()

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("download", download_command_handler)) # New download handler


    # Add message handler for video URLs (non-command text messages)
    # application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_url))

    # Run the bot until the user presses Ctrl-C
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()