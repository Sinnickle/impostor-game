from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import os

app = Flask(__name__)
socketio = SocketIO(app)

# Home page route
@app.route("/")
def home():
    return render_template("index.html")

# Create game page
@app.route("/create")
def create():
    return "<h2>Create Game Page</h2>"

# Join game page
@app.route("/join")
def join():
    return "<h2>Join Game Page</h2>"

# Example real-time event
@socketio.on("message")
def handle_message(data):
    print("Received:", data)
    emit("message", data, broadcast=True)

if __name__ == "__main__":
    print("Flask-SocketIO is starting…")
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)