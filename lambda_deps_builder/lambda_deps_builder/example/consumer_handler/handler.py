import requests


def handler(event, context):
    response = requests.get("https://example.com", timeout=5)
    return {"status": response.status_code, "len": len(response.text)}
