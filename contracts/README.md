# Contracts — Smart Contracts (Solidity/Hardhat)

Mã nguồn Smart Contracts cho hệ thống HRC Safety Log.

## Nội dung

```text
contracts/
├── contracts/          # 4 file Solidity
│   ├── HRCSafetyLog.sol
│   ├── HRCRewardToken.sol
│   ├── HRCSessionRewards.sol
│   └── HRCTaskRewards.sol
├── scripts/            # Deploy Hardhat local/testnet
├── test/               # Kiểm thử Hardhat
├── deployments/        # Manifest địa chỉ contract
├── package.json
└── hardhat.config.js
```

Nguồn gốc: copy từ `smart_contracts/` của monorepo phát triển (bỏ
`node_modules/`, `artifacts/`, `cache/`).

## Cài đặt và chạy Demo

```bash
cd contracts
npm ci
npx hardhat compile
npx hardhat test
# Chain local:
npx hardhat node
npx hardhat run scripts/deploy.js --network localhost
```
