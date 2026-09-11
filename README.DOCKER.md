Run with Docker (development)

Build and start the app using Docker Compose:

```bash
docker compose up --build
```

This will build the image, install dependencies inside the container, and start the FastAPI server at http://127.0.0.1:8000.

Notes:
- The repository is mounted into the container so code edits are visible (dev reload enabled).
- The app stores data in Supabase (PostgreSQL). Create `.env` with `DATABASE_URL` before starting; it reaches the container through the `./:/app` mount and is excluded from the image by `.dockerignore`.
