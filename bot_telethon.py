#!/usr/bin/env python
import logging
import os
import re
import asyncio
import subprocess # will run it in a thread
from functools import partial # For progress hook

from telethon import TelegramClient, events
from telethon.tl.types import BotCommandScopeDefault, DocumentAttributeFilename, BotCommand as TelethonBotCommand
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.utils import get_display_name
from dotenv import load_dotenv

load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH]):
    logger.critical("Missing one or more Telegram credentials (TOKEN, API_ID, API_HASH) in .env or environment.")
    exit(1)

try:
    TELEGRAM_API_ID = int(TELEGRAM_API_ID)
except ValueError:
    logger.critical("TELEGRAM_API_ID must be an integer.")
    exit(1)


DOWNLOAD_PATH = "video_downloads/"

YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "cookies/youtube.txt")
INSTAGRAM_COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE", "cookies/instagram.txt")

# --- Global store for chat-specific data (replacement for context.chat_data) ---
# This is a simple in-memory store. For persistence, you'd need a database.
chat_data_store = {}
# To store the main event loop for thread-safe coroutine execution
main_event_loop = None

# Initialize Telethon Client
client = TelegramClient('bot_session_subprocess', TELEGRAM_API_ID, TELEGRAM_API_HASH)

# --- Helper Functions ---
def ensure_download_path_exists():
    if not os.path.exists(DOWNLOAD_PATH):
        try:
            os.makedirs(DOWNLOAD_PATH)
            logger.info(f"Created download directory: {DOWNLOAD_PATH}")
        except OSError as e:
            logger.error(f"Error creating download directory {DOWNLOAD_PATH}: {e}")
            raise # Critical for operation

async def send_typing_action(chat_id):
    try:
        async with client.action(chat_id, 'upload_video'):
            await asyncio.sleep(0.5) # Keep action active for a bit, or let the long op run inside
    except Exception as e:
        logger.warning(f"Could not send typing action to {chat_id}: {e}")


def is_time_like(text: str) -> bool:
    if not text:
        return False
    return ':' in text or text.isdigit()

def is_youtube_url(url: str) -> bool:
    youtube_regex = (
        r'(https?://)?(www\.)?'
        '(youtube|youtu|youtube-nocookie)\.(com|be)/'
        '(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})')
    return bool(re.match(youtube_regex, url))

def is_instagram_url(url: str) -> bool:
    instagram_regex = r'(https?://)?(www\.)?instagram\.com/(p|reel|tv)/([^/?#&]+)'
    return bool(re.match(instagram_regex, url))

# --- yt-dlp Progress Hook (Modified for Telethon and threading) ---
def _download_progress_hook_sync(d, chat_id, initial_message_id_obj, client_ref, loop_ref):
    """
    Synchronous part of the hook called by yt-dlp.
    It schedules the async part to run on the main event loop.
    initial_message_id_obj is a list or dict to pass message_id by reference.
    """
    if not loop_ref or not loop_ref.is_running():
        logger.warning("Main event loop not available for progress hook.")
        return

    # Get chat data for this specific chat
    current_chat_data = chat_data_store.setdefault(chat_id, {})
    initial_message_id = initial_message_id_obj[0] # Get the actual message ID

    async def edit_message_async(text):
        if initial_message_id:
            try:
                await client_ref.edit_message(chat_id, initial_message_id, text)
            except Exception as e:
                # logger.warning(f"Hook: Could not edit progress message {initial_message_id} in chat {chat_id}: {e}")
                pass # Often "message not modified" or message deleted

    if d['status'] == 'downloading':
        percent_str = d.get('_percent_str', 'N/A').replace('\x1b[0;94m', '').replace('\x1b[0m', '')
        total_bytes_str = d.get('_total_bytes_str', 'N/A')
        speed_str = d.get('_speed_str', 'N/A')
        eta_str = d.get('_eta_str', 'N/A')
        current_progress_message = f"Downloading...\nProgress: {percent_str}\nSize: {total_bytes_str}\nSpeed: {speed_str}\nETA: {eta_str}"
        
        last_message = current_chat_data.get('last_progress_msg', "")
        if last_message != current_progress_message:
            asyncio.run_coroutine_threadsafe(edit_message_async(current_progress_message), loop_ref)
            current_chat_data['last_progress_msg'] = current_progress_message

    elif d['status'] == 'finished':
        logger.info(f"yt-dlp finished processing for chat {chat_id}. Filename info: {d.get('filename') or d.get('info', {}).get('_filename')}")
        final_filename = d.get('filename') or d.get('info', {}).get('_filename')
        if final_filename:
            current_chat_data['download_filename'] = final_filename
        
        asyncio.run_coroutine_threadsafe(edit_message_async("✅ Download finished by yt-dlp. Preparing to send..."), loop_ref)
        current_chat_data.pop('last_progress_msg', None)

    elif d['status'] == 'error':
        logger.error(f"yt-dlp reported an error for chat {chat_id}.")
        current_chat_data['download_error'] = True
        asyncio.run_coroutine_threadsafe(edit_message_async("❌ yt-dlp encountered an error during download."), loop_ref)


async def downloader_segment(event: events.NewMessage.Event, url: str,
                             start_time_str: str, end_time_str: str) -> tuple:
    chat_id = event.chat_id
    # Pass initial_message_id by mutable type (list) so hook can see updates if it's set later
    initial_message_id_container = [None] 

    if not url.startswith(('http://', 'https://')):
        await event.reply("⚠️ That doesn't look like a valid URL. Please send a direct link to a video.")
        return None, None

    processing_message = await event.reply("🔍 Got your link! Processing and trying to download the video...")
    initial_message_id_container[0] = processing_message.id
    await send_typing_action(chat_id)

    downloaded_file_path = None  # To store the path of the downloaded file

    # Get chat data for this specific chat
    current_chat_data = chat_data_store.setdefault(chat_id, {})
    current_chat_data.pop('download_filename', None) # Clear previous attempts
    current_chat_data.pop('download_error', None)

    try:
        # yt-dlp options
        ydl_opts = {
            'output': f'{DOWNLOAD_PATH}%(title)s.200B.%(ext)s', # Limit title length to avoid overly long filenames
            'no-playlist': "", # Download only single video if playlist URL is given
            # 'quiet': True, # Suppress yt-dlp console output
            'merge-output-format': 'mp4', # Ensure output is mp4 if merging is needed
            # 'verbose': True
        }

        if start_time_str and end_time_str:
            ydl_opts['download-sections'] = f"*{start_time_str}-{end_time_str}"
            ydl_opts['force-keyframes-at-cuts'] = "" # potential issue
            logger.info(f"Segment (start-end): recode=mp4, download_sections, force_keyframes, pp_args for FFmpegVideoConverter.")
        
        elif start_time_str:
            ydl_opts['download-sections'] = f"*{start_time_str}"
            ydl_opts['force-keyframes-at-cuts'] = ""
            logger.info(f"Segment (start-onwards): recode=mp4, download_sections, force_keyframes, pp_args with -ss for FFmpegVideoConverter.")
        
        else: # Full video download
            logger.info(f"Full video download requested for {url}")

        # if is_youtube_url(url) and YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
        #     ydl_opts['cookiefile'] = YOUTUBE_COOKIES_FILE
        # elif is_instagram_url(url) and INSTAGRAM_COOKIES_FILE and os.path.exists(INSTAGRAM_COOKIES_FILE):
        #     ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE    

        ydl_opts_list = []
        for k,v in ydl_opts.items():
            if k:
                ydl_opts_list.append(f"--{k}")
            if v:
                ydl_opts_list.append(v)

        cmd = ["yt-dlp", *ydl_opts_list, url]
        print(cmd)

        # def run_subprocess_sync(command_list):
        #     try:
        #         # Using Popen for more control if we wanted to read stdout/stderr live later
        #         # For now, `run` is fine. Capture output for logging.
        #         process = subprocess.run(command_list, check=False, capture_output=True, text=True, encoding='utf-8')
        #         if process.returncode == 0:
        #             logger.info(f"yt-dlp subprocess finished successfully for {url}.")
        #             logger.debug(f"yt-dlp stdout:\n{process.stdout}")
        #             # Try to find the downloaded file; yt-dlp doesn't return filename easily via CLI to the caller
        #             # We rely on outtmpl and then scan the directory.
        #             # This part is inherently less robust than the library's progress hook.
        #             # A more robust way would be for yt-dlp to print the final filename to stdout
        #             # and parse that, e.g., using --print filename
                    
        #             # Basic fallback: find the newest mp4/mkv/webm file in DOWNLOAD_PATH
        #             # This assumes only one download is happening at a time or filenames are unique enough.
        #             list_of_files = ([os.path.join(DOWNLOAD_PATH, f) for f in os.listdir(DOWNLOAD_PATH) if f.endswith(".mp4")])
        #             if list_of_files:
        #                 downloaded_file_path = max(list_of_files, key=os.path.getctime)
        #                 logger.info(f"Found downloaded file by fallback: {downloaded_file_path}")
        #                 return downloaded_file_path
        #             else:
        #                 logger.error(f"yt-dlp subprocess finished, but no output file found in {DOWNLOAD_PATH}.")
        #                 return None
        #         else:
        #             logger.error(f"yt-dlp subprocess failed for {url} with exit code {process.returncode}.")
        #             logger.error(f"yt-dlp stderr:\n{process.stderr}")
        #             logger.error(f"yt-dlp stdout:\n{process.stdout}")
        #             current_chat_data['download_error'] = True
        #             return None
        #     except FileNotFoundError:
        #         logger.error("yt-dlp command not found. Is it installed and in PATH?")
        #         current_chat_data['download_error'] = True
        #         return None
        #     except Exception as e_sync:
        #         logger.error(f"Error running yt-dlp subprocess for {url}: {e_sync}", exc_info=True)
        #         current_chat_data['download_error'] = True
        #         return None

        try:
            # # Run the blocking subprocess in a separate thread
            # downloaded_file_path = await asyncio.to_thread(run_subprocess_sync, cmd)
            subprocess.run(cmd) # potentially blocking...

            if not downloaded_file_path or not os.path.exists(downloaded_file_path):
                # try to find the latest mp4 file in the directory (less robust)
                list_of_files = [os.path.join(DOWNLOAD_PATH, f) for f in os.listdir(DOWNLOAD_PATH) if f.endswith(".mp4")]
                if list_of_files:
                    downloaded_file_path = max(list_of_files, key=os.path.getctime)
                    logger.info(f"Found downloaded file by fallback: {downloaded_file_path}")
                else:
                    logger.error("Could not determine downloaded file path after download.")
            if downloaded_file_path and os.path.exists(downloaded_file_path):
                file_size = os.path.getsize(downloaded_file_path)
                logger.info(f"File downloaded: {downloaded_file_path}, Size: {file_size / (1024*1024):.2f} MB")
            else:
                logger.error("Download via subprocess seemed to finish but no file path was determined.")
                current_chat_data['download_error'] = True # Mark as error
        except Exception as e: # Catch other yt-dlp related errors
            logger.error(f"yt-dlp generic error for URL {url}: {e}")
        
    except Exception as e: # Catch errors from asyncio.to_thread or other async issues
        logger.error(f"An overarching error occurred while processing URL {url} with subprocess: {e}", exc_info=True)
        current_chat_data['download_error'] = True
        downloaded_file_path = None

    if processing_message and current_chat_data.get('download_error'):
        try:
            await client.edit_message(chat_id, processing_message.id, "❌ yt-dlp encountered an error during download.")
        except: pass

    elif processing_message and downloaded_file_path:
         try:
            await client.edit_message(chat_id, processing_message.id, "✅ Download finished by yt-dlp. Preparing to send...")
         except: pass
    elif processing_message: # No file and no explicit error, implies failure
         try:
            await client.edit_message(chat_id, processing_message.id, "❌ Download failed or no file was produced.")
         except: pass

    return downloaded_file_path, processing_message


@client.on(events.NewMessage(pattern='/start'))
async def start_command_handler(event: events.NewMessage.Event):
    sender = await event.get_sender()
    sender_name = get_display_name(sender)
    welcome_message = (
        f"👋 Hello <a href='tg://user?id={sender.id}'>{sender_name}</a>!\n\n"
        "I'm your video downloading bot.\n"
        "Use <code>/download <URL> [START_TIME] [END_TIME]</code> to fetch a video or a segment.\n"
        "Times are optional (e.g., <code>MM:SS</code> or <code>HH:MM:SS</code>).\n\n"
        "Example (full video): <code>/download <your_video_url></code>\n"
        "Example (segment): <code>/download <your_video_url> 00:10 00:50</code>\n\n"
        "Type /help for more detailed information."
    )
    await event.reply(welcome_message, parse_mode='html')

@client.on(events.NewMessage(pattern='/help'))
async def help_command_handler(event: events.NewMessage.Event):
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
    await event.reply(help_text, parse_mode='md')


@client.on(events.NewMessage(pattern=r'/download(?: |$)(.*)'))
async def download_command_tele_handler(event: events.NewMessage.Event):
    chat_id = event.chat_id
    args_str = event.pattern_match.group(1).strip()
    
    if not args_str:
        await event.reply("⚠️ URL missing. Usage: /download <URL> [start] [end]")
        return

    args = args_str.split()
    url = args[0]
    
    start_time_str = None
    end_time_str = None

    if len(args) >= 2:
        potential_start_time = args[1]
        if is_time_like(potential_start_time):
            start_time_str = potential_start_time
        else:
            await event.reply(f"⚠️ '{potential_start_time}' doesn't look like a valid start time (e.g., MM:SS). Proceeding without start time.")
            # If first time-like arg is invalid, don't assume others are times for segment
            # url = " ".join(args) # Reconstruct URL if it contained spaces and wasn't the only arg
            # args = [url] # Reset args to just URL

    if len(args) >= 3 and start_time_str: # Only look for end_time if start_time was plausible
        potential_end_time = args[2]
        if is_time_like(potential_end_time):
            end_time_str = potential_end_time
        else:
            await event.reply(f"⚠️ '{potential_end_time}' doesn't look like a valid end time. Will download from {start_time_str} to end if applicable.")

    downloaded_file_path, processing_message = await downloader_segment(event, url, start_time_str, end_time_str)
    
    # Get chat data for this specific chat
    current_chat_data = chat_data_store.get(chat_id, {})


    try:
        if downloaded_file_path and os.path.exists(downloaded_file_path):
            if processing_message:
                 await client.edit_message(chat_id, processing_message.id, "✅ Download complete! Now uploading to Telegram...")
            await send_typing_action(chat_id)

            try:
                # Use client.send_file for Telethon
                # The 'video' parameter in PTB's send_video becomes 'file' in Telethon's send_file
                # PTB's InputFile is not directly used; Telethon handles paths or bytesIO.
                sent_message = await client.send_file(
                    chat_id,
                    downloaded_file_path,
                    caption=f"🎬 Here's your video!\nOriginal URL: {url}",
                    supports_streaming=True,
                    attributes=[DocumentAttributeFilename(os.path.basename(downloaded_file_path))]
                )
                if processing_message:
                    await client.delete_messages(chat_id, processing_message.id)
                logger.info(f"Video sent to chat_id {chat_id}: {downloaded_file_path}")

            except Exception as e:
                logger.error(f"Error sending video to Telegram: {e}", exc_info=True)
                if processing_message:
                    await client.edit_message(chat_id, processing_message.id, f"❌ Failed to upload video to Telegram: {str(e)}")
        else:
            if not current_chat_data.get('download_error'): # If no specific error set by hook
                if processing_message:
                    await client.edit_message(chat_id, processing_message.id, "❌ Download failed or no file was produced. Please check the URL or try again.")
            # Error message already sent by hook or above if processing_message was None
            elif processing_message and not current_chat_data.get('download_error'): # Error from downloader_segment itself
                 await client.edit_message(chat_id, processing_message.id, "❌ Download failed. Please check logs.")


    except Exception as e:
        logger.error(f"An overarching unexpected error occurred for URL {url}: {e}", exc_info=True)
        if processing_message:
            try:
                await client.edit_message(chat_id, processing_message.id, "❌ An unexpected error occurred. Please try again later.")
            except: pass # Ignore if editing fails
    finally:
        if downloaded_file_path and os.path.exists(downloaded_file_path):
            try:
                os.remove(downloaded_file_path)
                logger.info(f"Cleaned up downloaded file: {downloaded_file_path}")
            except OSError as e:
                logger.error(f"Error deleting file {downloaded_file_path}: {e}")
        
        # Clean up chat_data_store for this chat
        current_chat_data.pop('download_filename', None)
        current_chat_data.pop('download_error', None)
        current_chat_data.pop('last_progress_msg', None)

async def set_bot_commands(client_instance):
    commands = [
        TelethonBotCommand("start", "Starts the bot and shows a welcome message."),
        TelethonBotCommand("help", "Shows the help message with instructions."),
        TelethonBotCommand("download", "Downloads a video or segment. Usage: /download <URL> [start] [end]")
    ]
    try:
        await client_instance(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code='en',
            commands=commands
        ))
        logger.info("Bot commands have been set programmatically using Telethon.")
    except Exception as e:
        logger.error(f"Failed to set bot commands: {e}")

async def main():
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()

    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN": # Redundant check given earlier exit
        logger.error("CRITICAL: Bot token is not set!")
        return
    
    if YOUTUBE_COOKIES_FILE and not os.path.exists(YOUTUBE_COOKIES_FILE):
        logger.warning(f"YouTube cookies file configured but not found at '{YOUTUBE_COOKIES_FILE}'. YouTube downloads might fail for private videos.")
    elif YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
        logger.info(f"YouTube cookies file found at '{YOUTUBE_COOKIES_FILE}'. Will be used for YouTube URLs.")

    if INSTAGRAM_COOKIES_FILE and not os.path.exists(INSTAGRAM_COOKIES_FILE):
        logger.warning(f"Instagram cookies file configured but not found at '{INSTAGRAM_COOKIES_FILE}'. Instagram downloads might fail for private content.")
    elif INSTAGRAM_COOKIES_FILE and os.path.exists(INSTAGRAM_COOKIES_FILE):
        logger.info(f"Instagram cookies file found at '{INSTAGRAM_COOKIES_FILE}'. Will be used for Instagram URLs.")

    ensure_download_path_exists()

    try:
        logger.info("Starting bot...")
        # Start client, authenticating as a bot
        await client.start(bot_token=TELEGRAM_BOT_TOKEN)
        logger.info("Bot client started.")
        
        await set_bot_commands(client)

        logger.info("Bot is up and running. Press Ctrl+C to stop.")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Error during bot execution: {e}", exc_info=True)
    finally:
        if client.is_connected():
            await client.disconnect()
        logger.info("Bot stopped.")

if __name__ == "__main__":
    asyncio.run(main())