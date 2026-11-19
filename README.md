# UPI Fraud Monitor

A simulated UPI workspace with React, React Bits, SQLite, hosted NVIDIA models, and CopilotKit OpenBot. The upstream [Laya decision engine](https://github.com/NandhaKishorM/laya) runs locally on CPU. It screens each transfer and decides whether to settle or reverse after a six-agent investigation.

## Run the workspace

Start Docker Desktop, then run this from the project directory in PowerShell 7.

```powershell
.\openbot\setup.ps1 -Start
```

Open the workspace address printed by setup. The default is `http://127.0.0.1:8000`. For the same-network demo, `UPI_BIND_IP` in the private configuration contains the host’s Wi-Fi IPv4 address. First startup downloads the pinned checkpoint and waits for Laya to load. Docker keeps the model cache in a named volume.

Sign in as `bank` using its password from `data/demo.signins.json` and leave **Remember me** selected. The top-right picker or **Ctrl+K** switches between Bank, Sender, and each Receiver profile in that browser. The remembered cookie lasts up to one year. **Log out** immediately revokes the session. Dedicated sender and receiver credentials open only their own screens.

## Follow a payment

1. Switch to Sender and submit a transfer. The receipt shows screening, a hold when applicable, and the outcome.
2. Switch to Bank operations and select the transfer. Its case conversation shows each agent’s recommendation, the receiving agent, and the model ID.
3. Use **Watch in OpenBot**, then paste the supplied `Watch <case ID>` prompt into Laya Case Monitor. `Watch` streams arriving turns and the outcome. `Review <case ID>` replays the messages already stored.
4. Switch to the receiver to see credited funds. A reversal returns the reservation to the sender.

To replenish the demo sender, sign in as Bank, select **Reset sender balance**, and confirm the reset to ₹100,000. The Bank page records the reset and keeps all payment history and receiver balances. Wait for any reserved funds to settle or reverse first.

The app reserves funds while Laya chooses `clear` or `hold`. A clear assessment settles immediately. On a hold, the Investigation Coordinator and five specialists read prior messages and each publish an advisory assessment. Laya uses the evidence and discussion to choose `settle` or `reverse`. Bank operations shows the checkpoint, the raw choice, and its model probabilities. These probabilities have not been calibrated on UPI fraud labels. A top choice below 70% routes screening to investigation and the final decision to reversal. Release after a hold also requires a complete panel and at least one reviewer supporting release. Expiry or an unavailable decision engine returns the reservation. Transactional leases and idempotency keys protect ledger writes.

## Private configuration

Run `.\openbot\setup.ps1` without `-Start` to create `data/openbot.env`. Provide the coordinator’s NVIDIA key in `UPI_LLM_API_KEY` and the CopilotKit project key in `CPK_INTELLIGENCE_API_KEY`. Put the five other NVIDIA keys in the `UPI_AGENT_…_KEY` variables from [the template](openbot/env.example). Laya uses a public checkpoint and needs no API key. Model assignments appear under **How it works**.

The team demo uses simulated funds and local HTTP. Provider credentials, demo passwords and SQLite files stay in the Git-ignored `data/` directory.

## Local development

```powershell
npm ci
npm run build
python -m unittest discover -s tests -v
```

Use the Docker startup command above for the full local payment flow. For a host-only Python run with `.\run-demo.ps1`, provide the Laya service on loopback port 8090.

[React Bits and other interface sources](UI-LICENSES.md)
