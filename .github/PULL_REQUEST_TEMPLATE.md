## Summary

<!-- Briefly describe what this PR changes and why. -->

## Checklist

- [ ] Frontend typecheck & build pass (`cd frontend && pnpm typecheck && pnpm build`)
- [ ] Backend test suite passes (`.venv/bin/pytest -q`)
- [ ] Compose config validates (`docker compose config --quiet`)
- [ ] No `.env`, `.fernet_key`, SQLite databases, or private host/seedbox details committed
