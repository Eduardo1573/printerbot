AUTH_REQUIRED = "🔐 Authentication required.\n\nSend the access code to continue."
AUTH_SUCCESS = "✅ Authentication successful."
AUTH_INVALID = "❌ Invalid access code."
AUTH_ALREADY = "✅ You are already authenticated."
AUTH_LOCKED = "⛔ Too many invalid attempts. Try again in {minutes} min."
LOGGED_OUT = "👋 You have been logged out.\n\nSend /start to authenticate again."
NOT_LOGGED_IN = "You are not logged in."

WELCOME = "🖨 Printer bot is ready.\n\nSend a PDF to print it, or press “📥 Receive Scan” before scanning."

HELP = (
    "<b>How to use</b>\n\n"
    "🖨 <b>Print</b> — send a PDF file (as a document). It is printed once, exactly one copy.\n"
    "Limits: {max_mb} MB, {max_pages} pages.\n\n"
    "📥 <b>Receive Scan</b> — {scan_help}\n\n"
    "/start — show the menu\n"
    "/help — this help\n"
    "/logout — remove your access"
)
SCAN_HELP_EMAIL = (
    "press it, then start “Scan to E-mail” on the printer's touchscreen. "
    "The next scan is delivered to you. If several people are waiting, scans are delivered in order."
)
SCAN_HELP_ESCL = "press it, place the document on the scanner, then press “▶️ Start scan”."
SCAN_HELP_DISABLED = "scanning is not enabled on this installation."

PRINT_PROMPT = "📄 Send the PDF you want to print as a file.\n\nEvery document is printed exactly once (1 copy)."
PRINT_RECEIVED = "📄 Document received.\n\n🖨 Printing..."
PRINT_QUEUED = "📄 Document received.\n\n⏳ Another job is printing — yours is next in line..."
PRINT_SUCCESS = "✅ Printed successfully."
PRINT_FAILED = "❌ Printing failed.\n\nReason: {reason}"
PRINT_NOT_PDF = "❌ Only PDF files can be printed. Send the PDF as a file (📎 → File)."
PHOTO_NOT_SUPPORTED = "❌ Photos can't be printed. Send a PDF file instead."

SCAN_READY_EMAIL = (
    "📥 Ready to receive a scan.\n\n"
    "Place your document in the scanner and start the scan "
    "(Scan → E-mail on the printer).\n\n"
    "Waiting for the scanner... (expires in {minutes} min)"
)
SCAN_READY_EMAIL_QUEUED = (
    "📥 Scan request queued.\n\n"
    "{ahead} request(s) ahead of you. Scans are delivered in order; "
    "yours is scan #{position}.\n\n"
    "Expires in {minutes} min."
)
SCAN_READY_ESCL = (
    "📥 Ready to receive a scan.\n\n"
    "Place your document on the scanner glass{feeder}, then press “▶️ Start scan”.\n\n"
    "Expires in {minutes} min."
)
SCAN_ALREADY_PENDING = "📥 You already have an active scan request (position #{position}). Waiting for the scanner..."
SCAN_RECEIVED = "📄 Scan received."
SCAN_SCANNING = "🔄 Scanning..."
SCAN_CANCELLED = "✖️ Scan request cancelled."
SCAN_NOTHING_TO_CANCEL = "There is no pending scan request."
SCAN_FAILED = "❌ Scan failed.\n\nReason: {reason}"
SCAN_DISABLED = "📥 Scanning is not enabled on this installation."

UNKNOWN_COMMAND = "🤔 Unknown command. Use the buttons below or /help."
UNKNOWN_MESSAGE = "🤔 I can only print PDF files. Send a PDF or use the buttons below."
INTERNAL_ERROR = "⚠️ Something went wrong. Please try again later."
SERVICE_UNAVAILABLE = "⚠️ The service is temporarily unavailable (database). Please try again in a minute."
