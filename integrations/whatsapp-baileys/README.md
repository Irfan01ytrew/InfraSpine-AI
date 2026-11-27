# WhatsApp QR connector for Agent Studio

This optional local bridge uses the open-source `@whiskeysockets/baileys`
WhatsApp Web client. It connects the selected enabled Agent Studio agent to the
owner's **Message yourself** chat. It does not require Meta developer
credentials, a public webhook, or a tunnel.

## Install

Install Node.js 20 or newer, then in this directory run:

```powershell
npm ci --omit=dev --omit=optional
```

The Python application supervises the local bridge. It opens no network port;
its JSON protocol uses private process pipes. All credentials and WhatsApp
Signal keys are encrypted with the app's AES-GCM master key before they reach
PostgreSQL. Do not rotate or lose `MASTER_ENCRYPTION_KEY` without re-linking
WhatsApp.

## Link your phone

1. In InfraSpine AI, open **AI Agents → WhatsApp** and choose **QR se connect ·
   Baileys**.
2. Select your enabled agent and click **Show QR / Reconnect**.
3. On the phone account you intend to connect, open **WhatsApp → Linked devices
   → Link a device** and scan the QR shown inside InfraSpine AI.
4. After the dashboard shows **Connected**, open that account's **Message
   yourself** chat and send, for example:
   `/agent mera pending kaam batao`

Use `/agent` before each request. Other contacts, groups, broadcasts, old
messages and media are ignored. Agent tools and approvals follow the agent's
configured permissions. Keep this server running while using the connection.
Disconnect in InfraSpine AI to stop it, or revoke the linked device in WhatsApp.

## Service terms

Baileys is an unofficial client of WhatsApp Web, is not affiliated with or
endorsed by WhatsApp/Meta, and use may be affected by WhatsApp service changes
or account restrictions. Read and follow applicable terms. Pairing requires
the account owner to scan the QR; the connector cannot pair a phone remotely.
