# HRC Safety Log - Blockchain layer

Smart contract `HRCSafetyLog.sol` (Solidity 0.8.27, Hardhat).

## Tính năng chính
- Lưu hash dữ liệu cảm biến (bytes32), timestamp cảm biến và `recordedAt` (block.timestamp).
- **Tự động Emergency Stop**: `riskLevel = 3` khóa robot ngay trong giao dịch ghi event.
- `triggerEmergencyStop` thủ công: event phải đúng robot và risk >= WARNING.
- **Định danh thiết bị**: `registerDevice` / `revokeDevice`, chữ ký EIP-712 (`recordSafetyEventSigned`).
  Thiết bị đã đăng ký bắt buộc gửi kèm chữ ký; operator chỉ chuyển tiếp, không sửa được dữ liệu.
- `getLockInfo`: ai khóa, lúc nào, do event nào. `canOperate(deviceId)` để IoT hỏi trước khi chạy.
- Phân quyền Owner/Operator, chuyển quyền owner 2 bước (`transferOwnership` + `acceptOwnership`).

## Chạy local
```bash
cd smart_contracts
npm ci
npx hardhat test                       # 49 test
npx hardhat coverage                   # báo cáo coverage
REPORT_GAS=true npx hardhat test       # báo cáo gas (PowerShell: $env:REPORT_GAS="true"; npx hardhat test)
npx hardhat node                       # terminal 1
npx hardhat run scripts/deploy.js --network localhost   # terminal 2
```
`deploy.js` ghi địa chỉ vào `deployments/<network>.json`; blockchain_api tự đọc file này.

## Deploy Sepolia + Etherscan
1. Copy `.env.example` thành `.env`, điền `SEPOLIA_RPC_URL`, `DEPLOYER_PRIVATE_KEY` (ví test), `ETHERSCAN_API_KEY`.
2. `npx hardhat run scripts/deploy.js --network sepolia`
3. `npx hardhat verify --network sepolia <ADDRESS>`
4. Blockchain API: đặt `HRC_BLOCKCHAIN_NETWORK=sepolia`, `RPC_URL` và
   `PRIVATE_KEY` trong `blockchain_api/.env`.
