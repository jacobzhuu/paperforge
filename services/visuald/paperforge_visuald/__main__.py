import uvicorn


def main() -> None:
    uvicorn.run("paperforge_visuald.main:app", host="0.0.0.0", port=8082)


if __name__ == "__main__":
    main()
