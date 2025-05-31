# Dockerfile

# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
# - ffmpeg is crucial for yt-dlp for merging formats and segment downloads
# - git is sometimes needed by yt-dlp for updates or certain extractors (optional but good to have)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container at /app
# This assumes your bot script is named bot.py and is in the same directory as the Dockerfile
# Adjust if your main script has a different name or is in a subdirectory.
COPY . .
# If your bot script is e.g. telegram_ytdlp_bot.py, use:
# COPY telegram_ytdlp_bot.py .
# COPY any_other_helper_files_if_any .

# Expose a volume for downloads (optional, but good for persisting data)
# The bot script currently uses "video_downloads/" relative to its execution path.
# So, /app/video_downloads/ will be the path inside the container.
VOLUME /app/video_downloads

# Expose a volume for cookies (optional, for easier management)
VOLUME /app/cookies

# Define environment variables for configuration (these will be set at runtime)
# You can set defaults here, but it's better to pass them during `docker run`
ENV TELEGRAM_BOT_TOKEN=""
ENV YOUTUBE_COOKIES_FILE_PATH="/app/cookies/youtube.txt"
ENV INSTAGRAM_COOKIES_FILE_PATH="/app/cookies/instagram.txt"
# The bot script will need to be updated to use these _FILE_PATH variables

# Command to run the application
# Replace bot.py with the actual name of your main Python script
CMD ["python", "bot.py"]