# LocalLabel

LocalLabel is a local-first web application for designing, previewing, and
printing labels on TSPL-compatible thermal printers. It keeps templates,
graphics, and printer settings on your own machine or server—no cloud account
required.

## Features

- Build label templates in millimetres for 203, 300, or 600 DPI printers.
- Add text, images, QR codes, barcodes, and simple shapes to a template.
- Keep a searchable image library with groups and typed metadata.
- Preview the final label and download its TSPL program.
- Send TSPL to a browser WebUSB printer or to a server-visible raw USB, CUPS,
  libusb, or Windows printer.
- Use Russian or English in the interface.

## Quick start

Local development requires [Python](https://www.python.org/) 3.14 or newer and
[uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:ibvadim/local-label.git
cd local-label
uv sync
uv run uvicorn src.main:app --reload --port 8811
```

Open <http://127.0.0.1:8811>. The application creates its SQLite database at
`data/locallabel.db` on first start. The whole `data/` directory is ignored by
Git because it can contain your images and label data.

## Run with Docker

The published image is the recommended way to run LocalLabel. It requires only
Docker—no source checkout and no local build:

```bash
docker run -d \
  --name locallabel \
  --restart unless-stopped \
  -p 8811:8811 \
  -v locallabel-data:/data \
  ghcr.io/ibvadim/local-label:latest
```

Open <http://127.0.0.1:8811>. Docker keeps templates, assets, and printer
settings in the `locallabel-data` volume. To use another host port, replace
the first `8811` in `-p 8811:8811`, for example with `-p 8080:8811`.

### Docker Compose

If you prefer Compose, download only
[`compose.yaml`](https://raw.githubusercontent.com/ibvadim/local-label/main/compose.yaml)
and run:

```bash
docker compose up -d
```

Set `LOCALLABEL_PORT` to change the published port:

```bash
LOCALLABEL_PORT=8080 docker compose up -d
```

The `latest` image is rebuilt after every push to `main`. For predictable
deployments, use a version tag such as `ghcr.io/ibvadim/local-label:0.1.0`.

### Direct USB from a Linux Docker host

Create a stable udev symlink for the printer, then pass it to the optional
Compose override:

```bash
LOCALLABEL_PRINTER_DEVICE=/dev/locallabel-printer \
  docker compose -f compose.yaml -f compose.usb.yaml up -d
```

Direct USB pass-through does not work reliably with Docker Desktop on macOS or
Windows. On those systems, use CUPS, the Windows print spooler, or run
LocalLabel directly on the host.

## Configuration

All settings are optional.

| Variable | Purpose | Default |
| --- | --- | --- |
| `LOCALLABEL_DATABASE_URL` | Any SQLAlchemy database URL | `sqlite:///data/locallabel.db` |
| `LOCALLABEL_UNICODE_FONT` | Path to a TrueType font used when rasterizing Unicode text | System font fallback |
| `LOCALLABEL_PORT` | Published Docker port | `8811` |
| `LOCALLABEL_PRINTER_DEVICE` | Linux USB device passed by `compose.usb.yaml` | — |

For Windows server-side printing, install the optional `pywin32` package in the
environment that runs LocalLabel.

## Using LocalLabel

1. Upload graphics in **Icons** and optionally organize them into groups.
2. Create a label in **Templates**; template geometry is stored in printer dots
   while the physical label dimensions remain in millimetres.
3. Open **Print**, fill in input fields, inspect the preview, and either
   download TSPL or send it to a selected printer.

The application currently targets TSPL/TSPL2 printers. QR and barcode fields
are visual placeholders in the preview and should be verified on the intended
printer before production use.

## Development notes

The frontend is intentionally dependency-light static HTML/CSS/JavaScript,
served by FastAPI. The template editor loads Konva from jsDelivr, so its editor
needs network access unless you vendor that script for an offline deployment.

To build and run the checkout locally with Compose, use the development
override:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up --build
```

Run a lightweight smoke check after changes:

```bash
uv run python -c "from src.main import app; print(app.title)"
```

## License

LocalLabel is released under the [MIT License](LICENSE).
