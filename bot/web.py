from flask import Flask
import threading
import os

web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "Bot is running!", 200

def run_web():
    port = int(os.environ.get("PORT", 5000))
    web_app.run(host='0.0.0.0', port=port)

def start_health_check():
    threading.Thread(target=run_web, daemon=True).start()
