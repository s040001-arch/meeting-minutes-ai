from fastapi import FastAPI, Request

app = FastAPI()


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/callback")
async def callback(request: Request):
    body = await request.json()
    print(body)
    return {"status": "ok"}
