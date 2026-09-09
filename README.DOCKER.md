Run with Docker (development)

Build and start the app using Docker Compose:

```bash
docker compose up --build
```

This will build the image, install dependencies inside the container, and start the FastAPI server at http://127.0.0.1:8000.

Notes:
- The repository is mounted into the container so code edits are visible (dev reload enabled).
- Database `pattaya_tourism.db` will be created inside the container filesystem. To persist it, bind-mount a host directory to `/app` or add a named volume for the DB path.
