from flask import Flask
import threading
import os

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def start_health_check():
    threading.Thread(target=run_web, daemon=True).start()
