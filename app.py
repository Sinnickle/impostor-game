from flask import Flask, render_template
import os

app = Flask(__name__)

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

if __name__ == "__main__":
    print("Flask is starting…")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)