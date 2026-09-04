# Summit Station Yield - Streamlit Community Cloud

This package is prepared for deployment on Streamlit Community Cloud.

## Entry File

Use this file as the app entrypoint:

- `summit_station_yield_app_2.0_cloud.py`

## Required Files in This Folder

- `summit_station_yield_app_2.0_cloud.py`
- `requirements.txt`
- `.streamlit/config.toml`

## Auto Data Source Priority

This app supports multiple source modes in this priority order:

1. Manual file upload in web UI
2. Microsoft Graph folder sync from secrets
3. OneDrive direct file URLs from secrets
4. Local default paths (for localhost only)

## Microsoft Graph Mode (Recommended)

Use this mode if your OneDrive links open as HTML pages (not direct file bytes).

### Azure/Entra setup

1. Register an app in Microsoft Entra ID.
2. Add application permissions: `Files.Read.All` and `Sites.Read.All`.
3. Grant admin consent for the tenant.
4. Create a client secret.

### Configure Graph secrets in Streamlit Community Cloud

In app settings -> Secrets, paste:

```toml
[msgraph]
tenant_id = "<your-tenant-id-guid>"
client_id = "<your-app-client-id-guid>"
client_secret = "<your-app-client-secret>"
site_hostname = "seagatetechnology-my.sharepoint.com"
site_path = "/personal/qiuyue_c_cui_seagate_com"
drive_folder_path = "/Documents/Summit/Summit Yield Raw data"
file_extensions = [".csv"]
file_name_contains = "summit_raw_data"
max_files = 20
```

Parameter notes:

- `site_path`: from your personal site path (for your case, `/personal/qiuyue_c_cui_seagate_com`).
- `drive_folder_path`: OneDrive folder under drive root.
- `max_files`: newest N files to load (sorted by last modified time).

## OneDrive Direct URL Mode (Optional fallback)

Use this only if URLs are true direct-download links.

### Configure direct URL secrets

In app settings -> Secrets, paste:

```toml
[onedrive]
urls = [
  "https://<your-shared-direct-link-1>",
  "https://<your-shared-direct-link-2>"
]
```

You can also use one URL:

```toml
[onedrive]
url = "https://<your-shared-direct-link>"
```

## Deploy Steps

1. Create a new GitHub repository.
2. Upload all files from this folder to repository root.
3. Open Streamlit Community Cloud and create app from that repository.
4. Set main file path to `summit_station_yield_app_2.0_cloud.py`.
5. Add secrets as shown above.
6. Deploy.

## Notes

- Graph mode is the most reliable for SharePoint/OneDrive enterprise links.
- If using direct URL mode, links must be directly downloadable by the cloud runtime.
- Supported source file types: `.csv`, `.xlsx`, `.xlsm`, `.xls`.
- If Graph or OneDrive download fails, app shows traceback in an expandable error panel.
