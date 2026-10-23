from fastapi import FastAPI

# the admin is plain HTML pages, an auto-generated API page would only show the routes to strangers
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    return {"ok": True}
