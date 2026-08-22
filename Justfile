# Report generation / preview
report:
    uv run python scripts/export_report_data.py

report-check:
    uv run python scripts/export_report_data.py --check

serve:
    uv run python scripts/export_report_data.py
    uv run python -m http.server 8000 --directory site
