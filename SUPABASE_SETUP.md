# Supabase Setup for Allterra Webhook

## 1. Create Supabase Project

1. Go to https://supabase.com → sign up / log in
2. Create new project (free tier is fine for MVP)
3. Pick a region close to SA (Frankfurt or London)
4. Wait for project to provision (~2 mins)

## 2. Run Schema SQL

Open **SQL Editor** in Supabase dashboard, paste this, run it:

```sql
-- ── Analytics events (every webhook event, for the tracker dashboard) ──
CREATE TABLE IF NOT EXISTS analytics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant text NOT NULL DEFAULT 'default',
  event_type text NOT NULL,
  metadata jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_analytics_tenant_created
  ON analytics(tenant, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_analytics_event_type
  ON analytics(event_type);

-- ── Captured jobs / leads ──
CREATE TABLE IF NOT EXISTS jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant text NOT NULL DEFAULT 'default',
  client_name text,
  phone text,
  address text,
  description text,
  priority text DEFAULT 'normal',  -- 'high' | 'normal'
  source text DEFAULT 'call',      -- 'call' | 'whatsapp'
  status text DEFAULT 'new',       -- 'new' | 'actioned' | 'completed'
  call_summary text,
  call_id text,
  servcraft_id text,
  twenty_id text,
  captured_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_tenant_captured
  ON jobs(tenant, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_jobs_status
  ON jobs(status);

-- ── Feedback (post-job review responses) ──
CREATE TABLE IF NOT EXISTS feedback (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant text NOT NULL DEFAULT 'default',
  job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
  client_name text,
  phone text,
  sentiment text,                  -- 'happy' | 'unhappy'
  review_link_sent boolean DEFAULT false,
  review_link_clicked boolean DEFAULT false,
  escalated boolean DEFAULT false,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_feedback_tenant
  ON feedback(tenant, created_at DESC);
```

## 3. Get Your Keys

In Supabase dashboard → **Project Settings** → **API**:
- Copy **Project URL** (e.g. `https://abcdefg.supabase.co`)
- Copy **service_role** key (NOT anon — the service role has full read/write)

## 4. Add to Railway

Railway → your webhook service → **Variables** → add:

```
SUPABASE_URL = https://your-project.supabase.co
SUPABASE_SERVICE_KEY = your-service-role-key
```

Railway will redeploy automatically.

## 5. Verify

After redeploy, make a test call. Then in Supabase **Table Editor** → `analytics` table — you should see new rows for `call_answered`, `job_captured` etc.

If nothing shows up, check Railway logs for `log_event error` or `Supabase init failed` lines.

## Events Currently Logged

| Event | Trigger |
|---|---|
| `call_answered` | Retell call complete |
| `job_captured` | Call resulted in a lead |
| `emergency_escalated` | Call flagged as emergency |
| `whatsapp_message_received` | Inbound WA message |
| `whatsapp_missed_call` | WA call went unanswered |

More events will be added when post-job review automation and campaigns ship.
