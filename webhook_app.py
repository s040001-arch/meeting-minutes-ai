from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/callback")
def callback():
    return {"status": "ok"}
