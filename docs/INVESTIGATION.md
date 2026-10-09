# Investigation: can an HP M129–M134 print and scan for a remote server with no computer at home?

**Short answer:** yes, provided the **home router can run a site-to-site VPN client (WireGuard)**.
The printer can't open a tunnel itself and has no usable cloud API, so the router has to
provide connectivity. Scanning has two options, depending on the exact model.

## 1. What the printer can do (verified sources)

Primary source: HP user guide **c05208327**, *"LaserJet Pro MFP M129-M132, LaserJet Ultra MFP M133-M134"*
(<https://h10032.www1.hp.com/ctg/Manual/c05208327.pdf>), plus the HP M130fw datasheet.

| Capability | Fact | Source |
|---|---|---|
| Models | LED: M130a, M132a, M133a, M134a · 2-line LCD: M130nw, M132nw, M132snw, M130fn, M132fn, M132fp, M133fn, M134fn · Touchscreen: M130fw, M132fw | User guide, "Printer views" |
| Networking | "a" models are USB-only. All other models have Ethernet and/or Wi-Fi | User guide / datasheet |
| Print languages | **PCLmS, URF, PWG Raster**. No PDF, PostScript or PCL6 | HP M130fw datasheet |
| Driverless printing | AirPrint, Mopria (IPP on TCP 631) | User guide "Mobile printing", datasheet |
| Scan to E-mail | **Touchscreen models only** (M130fw, M132fw). SMTP server, port, optional SSL/TLS ("Always use secure connection") and SMTP AUTH are set in EWS → Scan → Outgoing E-mail Profiles | User guide ch. 6 |
| Scan to Network Folder | Touchscreen models only (SMB share) | User guide ch. 6 |
| Scan from 2-line/LED models | Only started from computer software (HP Scan / HP Easy Scan) | User guide ch. 6 |
| eSCL (AirPrint scanning) | **Not documented by HP for this series.** Community reports show an eSCL endpoint on port 8080 on the M132nw, with mixed results | HP forum; sane-airscan issue tracker |
| HP ePrint (e-mail to @hpeprint.com) | Needed HP Web Services and HP Connected. HP shut down the ePrint e-mail service in 2023. **Not usable** | HP support community |
| VPN / outbound tunnel | None. The printer only opens outbound connections for SMTP, SMB, DNS, NTP and HP Web Services | User guide (no such feature) |
| EWS security | Admin password: EWS → System → Administration → Product Security | User guide "Printer security features" |

## 2. What follows from that

1. **Printing has to be inbound to the printer.** Something has to reach `printer:631` (IPP). The
   printer is behind NAT and can't dial out, so the options are:
   * **Router-level VPN (chosen).** The router keeps a WireGuard tunnel to the VPS. The bot reaches
     `192.168.x.y:631` through it. Nothing is exposed on the Internet.
   * Router port-forward of 631 restricted to the VPS IP. This puts plaintext IPP (documents in
     clear text) on the Internet, so it is **not recommended**. It's documented in the README only
     as a last resort.
   * HP cloud (ePrint): discontinued. **Not possible.**
2. **PDFs must be rasterised on the VPS.** The printer only accepts PCLmS/URF/PWG raster.
   Ghostscript's `pwgraster` device renders the PDF, and the bot sends it with an IPP `Print-Job`
   with `copies=1`. This is what an AirPrint/IPP Everywhere client does. The bot reads
   `document-format-supported` at runtime and sends PDF directly if a printer ever supports it.
3. **Scanning depends on the model:**
   * **M130fw / M132fw**: the printer *pushes* scans with Scan to E-mail. Its SMTP server is set to
     the bot's built-in SMTP receiver on the VPS, reached through the tunnel. This is the only
     officially documented way for this series to send a scan anywhere without a computer. It also
     matches the "next scan belongs to the user who pressed 📥 Receive Scan" flow.
   * **nw / snw / fn / fp models**: the panel can't send scans anywhere. The only computer-free
     option is for the VPS to *pull* a scan over eSCL through the tunnel. HP doesn't document eSCL
     here, so treat it as **unverified until `python -m app.tools.probe --scan` succeeds** on the
     actual device. If it fails, scanning isn't possible on that model without a computer. Set
     `SCAN_MODE=disabled`.
   * **a models**: USB only, so nothing works without a computer.

## 3. Hard infrastructure requirement

The home router must:

* run a **WireGuard** client (or IPsec/OpenVPN with equivalent routing) to the VPS. Known to work:
  OpenWrt, MikroTik RouterOS 7, GL.iNet, ASUS (stock 388+/Merlin), FRITZ!Box 7.5+, Keenetic,
  UniFi. ISP-supplied routers often can't.
* allow **tunnel → printer** connections. Some "VPN client" modes treat the tunnel as WAN and
  block inbound traffic, which breaks printing.
* allow **printer → tunnel** connections (for Scan to E-mail).

If the router can't do this, the requirement can't be met as specified. The fix is a router that
can, such as a ~$30 GL.iNet travel router placed in front of the printer. That is still a router,
not a server.
