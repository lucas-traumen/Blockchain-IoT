// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

/// @title HRCSafetyLog - Hộp đen an toàn và định danh cho robot hợp tác
/// @notice Lưu vết hash sự kiện an toàn, tự động Emergency Stop khi DANGER,
///         định danh thiết bị bằng chữ ký EIP-712 (chống chối bỏ).
contract HRCSafetyLog {

    // ======================================================
    // CONSTANTS
    // ======================================================

    uint8 public constant RISK_WARNING = 2;
    uint8 public constant RISK_DANGER = 3;

    bytes32 private constant EIP712_DOMAIN_TYPEHASH = keccak256(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    );

    bytes32 private constant SAFETY_EVENT_TYPEHASH = keccak256(
        "SafetyEvent(string eventId,string deviceId,uint256 timestamp,bytes32 dataHash,uint8 riskLevel)"
    );

    // Nửa bậc của đường cong secp256k1: chặn chữ ký "dẻo" (malleable)
    uint256 private constant HALF_CURVE_ORDER =
        0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

    // ======================================================
    // ERRORS
    // ======================================================

    error NotOwner();
    error NotPendingOwner();
    error NotOperator();
    error InvalidAddress();
    error CannotRemoveOwner();
    error EmptyField();
    error InvalidRiskLevel();
    error EventAlreadyExists();
    error EventNotFound();
    error EventDeviceMismatch();
    error RiskTooLow();
    error RobotAlreadyLocked();
    error RobotNotLocked();
    error DeviceAlreadyRegistered();
    error DeviceNotRegistered();
    error DeviceIsRevoked();
    error SignatureRequired();
    error InvalidSignature();
    error IndexOutOfRange();

    // ======================================================
    // STORAGE
    // ======================================================

    struct SafetyEvent {
        string eventId;
        string deviceId;
        uint256 timestamp;   // thời điểm cảm biến đo (do client khai)
        uint256 recordedAt;  // thời điểm ghi lên chain (block.timestamp, không giả được)
        bytes32 dataHash;
        uint8 riskLevel;
        bool exists;
    }

    struct LockInfo {
        bool locked;
        string eventId;      // event gây ra lệnh khóa
        address lockedBy;
        uint256 lockedAt;
    }

    struct Device {
        address signer;      // địa chỉ (khóa công khai) của thiết bị / gateway
        bool revoked;
    }

    address public owner;
    address public pendingOwner;

    mapping(address => bool) public operators;

    mapping(string => SafetyEvent) private safetyEvents;
    string[] private eventIds;

    mapping(string => LockInfo) private locks;
    mapping(string => Device) private devices;

    mapping(string => uint256) public deviceEventCount;
    mapping(string => uint256) public lastEventAt;

    // ======================================================
    // EVENTS
    // ======================================================

    event SafetyEventRecorded(
        string eventId,
        string deviceId,
        uint256 timestamp,
        bytes32 dataHash,
        uint8 riskLevel
    );

    event EmergencyStopTriggered(
        string deviceId,
        string eventId,
        uint256 timestamp,
        address triggeredBy
    );

    event RobotUnlocked(
        string deviceId,
        uint256 timestamp,
        address unlockedBy
    );

    event OperatorAdded(address operator);
    event OperatorRemoved(address operator);

    event OwnershipTransferStarted(
        address indexed previousOwner,
        address indexed newOwner
    );
    event OwnershipTransferred(
        address indexed previousOwner,
        address indexed newOwner
    );

    event DeviceRegistered(string deviceId, address signer);
    event DeviceRevoked(string deviceId, uint256 timestamp);

    // ======================================================
    // MODIFIERS
    // ======================================================

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyOperator() {
        if (!operators[msg.sender]) revert NotOperator();
        _;
    }

    constructor() {
        owner = msg.sender;
        operators[msg.sender] = true;
    }

    // ======================================================
    // OWNERSHIP (2 bước: đề cử -> chấp nhận)
    // ======================================================

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert InvalidAddress();
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(owner, newOwner);
    }

    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert NotPendingOwner();

        address previousOwner = owner;
        owner = msg.sender;
        pendingOwner = address(0);
        operators[msg.sender] = true;

        emit OwnershipTransferred(previousOwner, msg.sender);
    }

    // ======================================================
    // OPERATORS
    // ======================================================

    function addOperator(address _operator) external onlyOwner {
        if (_operator == address(0)) revert InvalidAddress();
        operators[_operator] = true;
        emit OperatorAdded(_operator);
    }

    function removeOperator(address _operator) external onlyOwner {
        if (_operator == owner) revert CannotRemoveOwner();
        operators[_operator] = false;
        emit OperatorRemoved(_operator);
    }

    // ======================================================
    // DEVICE IDENTITY
    // ======================================================

    /// @notice Gắn định danh mật mã cho robot/gateway. Sau khi đăng ký,
    ///         mọi sự kiện của thiết bị này PHẢI có chữ ký hợp lệ.
    function registerDevice(string calldata deviceId, address signer)
        external
        onlyOwner
    {
        if (bytes(deviceId).length == 0) revert EmptyField();
        if (signer == address(0)) revert InvalidAddress();
        if (devices[deviceId].signer != address(0)) {
            revert DeviceAlreadyRegistered();
        }

        devices[deviceId] = Device({signer: signer, revoked: false});

        emit DeviceRegistered(deviceId, signer);
    }

    /// @notice Thu hồi quyền của thiết bị vi phạm / bị lộ khóa.
    function revokeDevice(string calldata deviceId) external onlyOwner {
        if (devices[deviceId].signer == address(0)) {
            revert DeviceNotRegistered();
        }

        devices[deviceId].revoked = true;

        emit DeviceRevoked(deviceId, block.timestamp);
    }

    function getDevice(string calldata deviceId)
        external
        view
        returns (address signer, bool revoked)
    {
        Device memory d = devices[deviceId];
        return (d.signer, d.revoked);
    }

    // ======================================================
    // RECORD EVENTS
    // ======================================================

    /// @notice Ghi sự kiện cho thiết bị CHƯA đăng ký (chế độ gateway tin cậy).
    function recordSafetyEvent(
        string calldata _eventId,
        string calldata _deviceId,
        uint256 _timestamp,
        bytes32 _dataHash,
        uint8 _riskLevel
    )
        external
        onlyOperator
    {
        if (devices[_deviceId].signer != address(0)) {
            revert SignatureRequired();
        }

        _record(_eventId, _deviceId, _timestamp, _dataHash, _riskLevel);
    }

    /// @notice Ghi sự kiện kèm chữ ký EIP-712 của thiết bị đã đăng ký.
    ///         Operator chỉ đóng vai trò chuyển tiếp (relay), không thể giả mạo dữ liệu.
    function recordSafetyEventSigned(
        string calldata _eventId,
        string calldata _deviceId,
        uint256 _timestamp,
        bytes32 _dataHash,
        uint8 _riskLevel,
        bytes calldata _signature
    )
        external
        onlyOperator
    {
        bytes32 digest = hashSafetyEvent(
            _eventId,
            _deviceId,
            _timestamp,
            _dataHash,
            _riskLevel
        );

        _verifyDeviceSignature(_deviceId, digest, _signature);

        _record(_eventId, _deviceId, _timestamp, _dataHash, _riskLevel);
    }

    function _record(
        string memory _eventId,
        string memory _deviceId,
        uint256 _timestamp,
        bytes32 _dataHash,
        uint8 _riskLevel
    )
        internal
    {
        if (
            bytes(_eventId).length == 0 ||
            bytes(_deviceId).length == 0 ||
            _dataHash == bytes32(0)
        ) {
            revert EmptyField();
        }

        if (_riskLevel > RISK_DANGER) revert InvalidRiskLevel();

        if (safetyEvents[_eventId].exists) revert EventAlreadyExists();

        safetyEvents[_eventId] = SafetyEvent({
            eventId: _eventId,
            deviceId: _deviceId,
            timestamp: _timestamp,
            recordedAt: block.timestamp,
            dataHash: _dataHash,
            riskLevel: _riskLevel,
            exists: true
        });

        eventIds.push(_eventId);

        deviceEventCount[_deviceId] += 1;
        lastEventAt[_deviceId] = block.timestamp;

        emit SafetyEventRecorded(
            _eventId,
            _deviceId,
            _timestamp,
            _dataHash,
            _riskLevel
        );

        // Smart Contract TỰ ĐỘNG khóa robot khi DANGER (cùng giao dịch, atomic)
        if (_riskLevel == RISK_DANGER) {
            _lock(_deviceId, _eventId);
        }
    }

    // ======================================================
    // EMERGENCY STOP / UNLOCK
    // ======================================================

    function _lock(string memory _deviceId, string memory _eventId)
        internal
    {
        if (locks[_deviceId].locked) return;

        locks[_deviceId] = LockInfo({
            locked: true,
            eventId: _eventId,
            lockedBy: msg.sender,
            lockedAt: block.timestamp
        });

        emit EmergencyStopTriggered(
            _deviceId,
            _eventId,
            block.timestamp,
            msg.sender
        );
    }

    /// @notice Khóa thủ công (leo thang). Event phải thuộc đúng robot và >= WARNING.
    function triggerEmergencyStop(
        string calldata _deviceId,
        string calldata _eventId
    )
        external
        onlyOperator
    {
        SafetyEvent storage e = safetyEvents[_eventId];

        if (!e.exists) revert EventNotFound();

        if (keccak256(bytes(e.deviceId)) != keccak256(bytes(_deviceId))) {
            revert EventDeviceMismatch();
        }

        if (e.riskLevel < RISK_WARNING) revert RiskTooLow();

        if (locks[_deviceId].locked) revert RobotAlreadyLocked();

        _lock(_deviceId, _eventId);
    }

    function unlockRobot(string calldata _deviceId) external onlyOwner {
        if (!locks[_deviceId].locked) revert RobotNotLocked();

        locks[_deviceId].locked = false;

        emit RobotUnlocked(_deviceId, block.timestamp, msg.sender);
    }

    // ======================================================
    // EIP-712
    // ======================================================

    function domainSeparator() public view returns (bytes32) {
        return keccak256(
            abi.encode(
                EIP712_DOMAIN_TYPEHASH,
                keccak256("HRCSafetyLog"),
                keccak256("1"),
                block.chainid,
                address(this)
            )
        );
    }

    function hashSafetyEvent(
        string calldata _eventId,
        string calldata _deviceId,
        uint256 _timestamp,
        bytes32 _dataHash,
        uint8 _riskLevel
    )
        public
        view
        returns (bytes32)
    {
        bytes32 structHash = keccak256(
            abi.encode(
                SAFETY_EVENT_TYPEHASH,
                keccak256(bytes(_eventId)),
                keccak256(bytes(_deviceId)),
                _timestamp,
                _dataHash,
                _riskLevel
            )
        );

        return keccak256(
            abi.encodePacked("\x19\x01", domainSeparator(), structHash)
        );
    }

    function _verifyDeviceSignature(
        string calldata _deviceId,
        bytes32 _digest,
        bytes calldata _signature
    )
        internal
        view
    {
        Device memory d = devices[_deviceId];

        if (d.signer == address(0)) revert DeviceNotRegistered();
        if (d.revoked) revert DeviceIsRevoked();

        if (_recover(_digest, _signature) != d.signer) {
            revert InvalidSignature();
        }
    }

    function _recover(bytes32 _digest, bytes calldata _sig)
        internal
        pure
        returns (address)
    {
        if (_sig.length != 65) revert InvalidSignature();

        bytes32 r = bytes32(_sig[0:32]);
        bytes32 s = bytes32(_sig[32:64]);
        uint8 v = uint8(_sig[64]);

        if (uint256(s) > HALF_CURVE_ORDER) revert InvalidSignature();
        if (v != 27 && v != 28) revert InvalidSignature();

        address signer = ecrecover(_digest, v, r, s);

        if (signer == address(0)) revert InvalidSignature();

        return signer;
    }

    // ======================================================
    // VIEWS
    // ======================================================

    function isRobotLocked(string calldata _deviceId)
        external
        view
        returns (bool)
    {
        return locks[_deviceId].locked;
    }

    /// @notice IoT gọi hàm này trước khi cho robot chạy.
    function canOperate(string calldata _deviceId)
        external
        view
        returns (bool)
    {
        return !locks[_deviceId].locked && !devices[_deviceId].revoked;
    }

    function getLockInfo(string calldata _deviceId)
        external
        view
        returns (
            bool locked,
            string memory eventId,
            address lockedBy,
            uint256 lockedAt
        )
    {
        LockInfo memory l = locks[_deviceId];
        return (l.locked, l.eventId, l.lockedBy, l.lockedAt);
    }

    function getSafetyEvent(string calldata _eventId)
        external
        view
        returns (
            string memory eventId,
            string memory deviceId,
            uint256 timestamp,
            bytes32 dataHash,
            uint8 riskLevel,
            uint256 recordedAt
        )
    {
        SafetyEvent memory e = safetyEvents[_eventId];

        if (!e.exists) revert EventNotFound();

        return (
            e.eventId,
            e.deviceId,
            e.timestamp,
            e.dataHash,
            e.riskLevel,
            e.recordedAt
        );
    }

    function getEventCount() external view returns (uint256) {
        return eventIds.length;
    }

    function getEventIdByIndex(uint256 index)
        external
        view
        returns (string memory)
    {
        if (index >= eventIds.length) revert IndexOutOfRange();
        return eventIds[index];
    }
}
