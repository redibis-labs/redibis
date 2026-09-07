c = get_config()  # noqa: F821 — injected by Jupyter at runtime

# --- Network ---
c.ServerApp.ip = '0.0.0.0'
c.ServerApp.port = 8888
c.ServerApp.allow_origin = '*'
c.ServerApp.allow_remote_access = True
c.ServerApp.open_browser = False

# --- Security (local dev — no token/password) ---
c.ServerApp.token = ''
c.ServerApp.password = ''
c.IdentityProvider.token = ''
c.ServerApp.disable_check_xsrf = True

# --- Performance ---
c.ServerApp.iopub_data_rate_limit = 1.0e10
c.ServerApp.rate_limit_window = 10
c.ServerApp.cookie_max_age_days = 30

# --- Paths ---
c.ServerApp.root_dir = '/home/jovyan/work'
c.ServerApp.notebook_dir = '/home/jovyan/work'

# --- Misc ---
c.ServerApp.terminals_enabled = True
c.ServerApp.allow_root = True
c.FileContentsManager.delete_to_trash = False

# --- Lab settings ---
c.LabApp.default_url = '/lab'
c.LabApp.dev_mode = False
c.LabApp.collaborative = False
