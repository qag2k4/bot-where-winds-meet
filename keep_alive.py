from flask import Flask
from threading import Thread
import logging

# Tắt bớt log của Flask để đỡ rối mắt console
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask('')

@app.route('/')
def home():
    return "I'm alive!"

def run():
    # Chạy server ở port 8080
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
