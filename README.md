# MetroFlow AI - Backend

AI-powered metro crowd management and scheduling platform.

## Quick start

```bash
python -m venv venv
# Windows: venv\Scripts\Activate
# macOS/Linux: source venv/bin/activate

pip install -r requirements.txt
```


```bash
python -m app.database.init_db     # create tables
python -m app.database.seed        # demo stations/trains/schedules (no login created)

        # add --reset to replace an existing synthetic seed

# Sign up through the frontend (Supabase), then make yourself admin:
python -m app.database.set_user_role <your-email> admin


uvicorn app.main:app --reload
```


## Auth flow

1. Frontend calls `supabase.auth.signUp()` / `signInWithPassword()`
   directly (see the frontend's `src/lib/supabase/client.ts` and
   `src/app/(auth)/login/page.tsx`). Supabase issues a JWT.
2. Frontend sends that JWT as `Authorization: Bearer <token>` on every
   API request (wire this into `src/lib/axios.ts` with an interceptor
   that reads `supabase.auth.getSession()`).
3. This backend verifies the JWT's signature (`app/core/security.py`)
   and, on the very first request from a given user, creates a
   matching row in `user_profiles` (default role: `passenger`).
4. To promote someone to `admin`/`operator`, run
   `python -m app.database.set_user_role <email> <role>` - this is the
   only way to create the *first* admin, since every role-changing API
   route itself requires you to already be an admin.

There is intentionally no `/api/v1/auth/register` or `/auth/login`
endpoint on this backend - sign-up and login happen entirely on
Supabase/the frontend. The only auth route here is `GET /api/v1/auth/me`.


