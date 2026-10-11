// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

interface IHRCRewardToken {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title HRCSessionRewards
/// @notice Session stake, duration-based safety reward and violation penalty.
/// @dev The contract does not mint tokens. It moves fixed-supply ERC-20 tokens
///      between users, this contract's reward pool, and the penalty treasury.
contract HRCSessionRewards {
    // ======================================================
    // SESSION REWARD POLICY
    // ======================================================

    // The project token uses 18 decimals, so 1 coin is 1 ether unit.
    uint256 public constant stakeAmount = 2 ether;
    // rewardAmount is the base reward at the 30-second validity threshold.
    uint256 public constant rewardAmount = 3 ether;
    uint256 public constant penaltyAmount = 2 ether;
    uint256 public constant MIN_REWARD_DURATION = 30 seconds;
    uint256 public constant FULL_BONUS_DURATION = 60 seconds;
    uint256 public constant MAX_REWARD_BONUS_PERCENT = 100;
    uint256 public constant SECONDS_PER_DAY = 1 days;

    // Outcome values submitted by the trusted backend/operator.
    uint8 public constant OUTCOME_VALID_NO_VIOLATION = 0;
    uint8 public constant OUTCOME_VIOLATION_CONFIRMED = 1;
    uint8 public constant OUTCOME_SENSOR_FAULT = 2;
    uint8 public constant OUTCOME_INCOMPLETE = 3;

    // Settlement status values returned by submitSessionOutcome/previewSettlement.
    uint8 public constant SETTLEMENT_REWARDED = 0;
    uint8 public constant SETTLEMENT_REWARD_DAILY_LIMIT = 1;
    uint8 public constant SETTLEMENT_REWARD_POOL_EMPTY = 2;
    uint8 public constant SETTLEMENT_PENALTY = 3;
    uint8 public constant SETTLEMENT_NO_REWARD = 4;

    // ======================================================
    // ERRORS
    // ======================================================

    error NotOwner();
    error NotPendingOwner();
    error NotOperator();
    error InvalidAddress();
    error InvalidAmount();
    error InvalidSessionId();
    error InvalidEvidenceHash();
    error InvalidTimestamp();
    error InvalidOutcome();
    error SessionAlreadyStarted();
    error SessionNotFound();
    error SessionAlreadySettled();
    error BeneficiaryMismatch();
    error CannotRemoveOwner();
    error TokenTransferFailed();

    // ======================================================
    // TYPES AND STORAGE
    // ======================================================

    struct Session {
        address beneficiary;
        uint256 startedAt;
        uint256 settledAt;
        uint256 stakedAmount;
        uint256 rewardPaid;
        uint256 penaltyCollected;
        uint8 outcome;
        uint8 settlementStatus;
        bytes32 evidenceHash;
        uint256 outcomeTimestamp;
        bool started;
        bool settled;
    }

    address public immutable token;
    address public immutable rewardPool;
    address public penaltyTreasury;

    address public owner;
    address public pendingOwner;
    mapping(address => bool) public operators;

    mapping(bytes32 => Session) public sessions;
    mapping(bytes32 => bool) public hasSettled;
    mapping(address => uint256) public lastRewardDay;

    // Accounting is separate from the token balance because the same contract
    // temporarily holds both reward-pool tokens and active session stakes.
    uint256 public rewardPoolBalance;
    uint256 public escrowedStakeTotal;

    uint256 private _reentrancyState = 1;

    // ======================================================
    // EVENTS
    // ======================================================

    event SessionStarted(
        bytes32 indexed sessionId,
        address indexed beneficiary,
        uint256 amount,
        uint256 startedAt
    );

    event SessionSettled(
        bytes32 indexed sessionId,
        address indexed beneficiary,
        uint8 outcome,
        uint8 settlementStatus,
        uint256 stakeReturned,
        uint256 rewardPaid,
        uint256 penaltyCollected,
        bytes32 evidenceHash,
        uint256 timestamp
    );

    event RewardPaid(
        bytes32 indexed sessionId,
        address indexed beneficiary,
        uint256 amount,
        uint256 rewardDay
    );

    event PenaltyCollected(
        bytes32 indexed sessionId,
        address indexed beneficiary,
        uint256 amount,
        address indexed treasury
    );

    event RewardPoolFunded(
        address indexed funder,
        uint256 amount,
        uint256 newBalance
    );

    event PenaltyTreasuryUpdated(
        address indexed previousTreasury,
        address indexed newTreasury
    );

    event OperatorAdded(address indexed operator);
    event OperatorRemoved(address indexed operator);
    event OwnershipTransferStarted(
        address indexed previousOwner,
        address indexed newOwner
    );
    event OwnershipTransferred(
        address indexed previousOwner,
        address indexed newOwner
    );

    // ======================================================
    // MODIFIERS / CONSTRUCTOR
    // ======================================================

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyOperator() {
        if (!operators[msg.sender]) revert NotOperator();
        _;
    }

    modifier nonReentrant() {
        if (_reentrancyState != 1) revert TokenTransferFailed();
        _reentrancyState = 2;
        _;
        _reentrancyState = 1;
    }

    constructor(address rewardToken, address treasury) {
        if (rewardToken == address(0) || treasury == address(0)) {
            revert InvalidAddress();
        }

        token = rewardToken;
        rewardPool = address(this);
        penaltyTreasury = treasury;
        owner = msg.sender;
        operators[msg.sender] = true;
    }

    // ======================================================
    // OWNERSHIP AND OPERATORS
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

    function addOperator(address operator) external onlyOwner {
        if (operator == address(0)) revert InvalidAddress();
        operators[operator] = true;
        emit OperatorAdded(operator);
    }

    function removeOperator(address operator) external onlyOwner {
        if (operator == owner) revert CannotRemoveOwner();
        operators[operator] = false;
        emit OperatorRemoved(operator);
    }

    function setPenaltyTreasury(address newTreasury) external onlyOwner {
        if (newTreasury == address(0)) revert InvalidAddress();

        address previousTreasury = penaltyTreasury;
        penaltyTreasury = newTreasury;
        emit PenaltyTreasuryUpdated(previousTreasury, newTreasury);
    }

    // ======================================================
    // POOL FUNDING
    // ======================================================

    /// @notice Deposit fixed-supply tokens into the reward pool.
    /// @dev The owner must approve this contract on the token first.
    function fundRewardPool(uint256 amount)
        external
        onlyOwner
        nonReentrant
    {
        if (amount == 0) revert InvalidAmount();
        _transferFrom(msg.sender, rewardPool, amount);

        rewardPoolBalance += amount;
        emit RewardPoolFunded(msg.sender, amount, rewardPoolBalance);
    }

    // ======================================================
    // SESSION STAKE
    // ======================================================

    /// @notice User starts a session and locks exactly stakeAmount() tokens.
    function startSession(bytes32 sessionId) external nonReentrant {
        _startSession(sessionId, msg.sender);
    }

    /// @notice Operator-assisted start for a beneficiary that approved this contract.
    function startSessionFor(bytes32 sessionId, address beneficiary)
        external
        onlyOperator
        nonReentrant
    {
        _startSession(sessionId, beneficiary);
    }

    function _startSession(bytes32 sessionId, address beneficiary) internal {
        if (sessionId == bytes32(0)) revert InvalidSessionId();
        if (beneficiary == address(0)) revert InvalidAddress();

        Session storage current = sessions[sessionId];
        if (current.started) revert SessionAlreadyStarted();
        if (hasSettled[sessionId]) revert SessionAlreadySettled();

        _transferFrom(beneficiary, rewardPool, stakeAmount);

        current.beneficiary = beneficiary;
        current.startedAt = block.timestamp;
        current.stakedAmount = stakeAmount;
        current.started = true;

        escrowedStakeTotal += stakeAmount;

        emit SessionStarted(
            sessionId,
            beneficiary,
            stakeAmount,
            block.timestamp
        );
    }

    // ======================================================
    // SESSION SETTLEMENT API
    // ======================================================

    /// @notice Canonical backend API for settling a session.
    /// @dev The contract maps outcome to amounts; the caller cannot choose amounts.
    function submitSessionOutcome(
        bytes32 sessionId,
        address beneficiary,
        uint8 outcome,
        bytes32 evidenceHash,
        uint256 timestamp
    )
        external
        onlyOperator
        nonReentrant
        returns (uint8 settlementStatus)
    {
        return _settleSession(
            sessionId,
            beneficiary,
            outcome,
            evidenceHash,
            timestamp
        );
    }

    /// @notice Compatibility alias with the same policy and parameter order.
    function settleSession(
        bytes32 sessionId,
        address beneficiary,
        uint8 outcome,
        bytes32 evidenceHash,
        uint256 timestamp
    )
        external
        onlyOperator
        nonReentrant
        returns (uint8 settlementStatus)
    {
        return _settleSession(
            sessionId,
            beneficiary,
            outcome,
            evidenceHash,
            timestamp
        );
    }

    function _settleSession(
        bytes32 sessionId,
        address beneficiary,
        uint8 outcome,
        bytes32 evidenceHash,
        uint256 timestamp
    ) internal returns (uint8 settlementStatus) {
        if (sessionId == bytes32(0)) revert InvalidSessionId();
        if (beneficiary == address(0)) revert InvalidAddress();
        if (evidenceHash == bytes32(0)) revert InvalidEvidenceHash();
        if (timestamp == 0) revert InvalidTimestamp();
        _validateOutcome(outcome);

        Session storage current = sessions[sessionId];
        if (!current.started) revert SessionNotFound();
        if (current.settled) revert SessionAlreadySettled();
        if (current.beneficiary != beneficiary) revert BeneficiaryMismatch();

        // Mark the session before token calls. nonReentrant protects the whole
        // operation, and a failed token transfer reverts all state changes.
        current.settled = true;
        current.settledAt = block.timestamp;
        current.outcome = outcome;
        current.evidenceHash = evidenceHash;
        current.outcomeTimestamp = timestamp;
        hasSettled[sessionId] = true;
        escrowedStakeTotal -= current.stakedAmount;

        uint256 stakeReturned;
        uint256 rewardPaid;
        uint256 penaltyCollected;

        if (outcome == OUTCOME_VALID_NO_VIOLATION) {
            _transferTo(beneficiary, current.stakedAmount);
            stakeReturned = current.stakedAmount;

            uint256 rewardDay = _rewardDay(block.timestamp);
            uint256 sessionReward = rewardAmountForDuration(
                block.timestamp - current.startedAt
            );
            if (lastRewardDay[beneficiary] == rewardDay) {
                settlementStatus = SETTLEMENT_REWARD_DAILY_LIMIT;
            } else if (rewardPoolBalance < sessionReward) {
                settlementStatus = SETTLEMENT_REWARD_POOL_EMPTY;
            } else {
                rewardPoolBalance -= sessionReward;
                _transferTo(beneficiary, sessionReward);
                lastRewardDay[beneficiary] = rewardDay;
                rewardPaid = sessionReward;
                settlementStatus = SETTLEMENT_REWARDED;

                emit RewardPaid(
                    sessionId,
                    beneficiary,
                    sessionReward,
                    rewardDay
                );
            }
        } else if (outcome == OUTCOME_VIOLATION_CONFIRMED) {
            _transferTo(penaltyTreasury, penaltyAmount);
            penaltyCollected = penaltyAmount;
            settlementStatus = SETTLEMENT_PENALTY;

            emit PenaltyCollected(
                sessionId,
                beneficiary,
                penaltyAmount,
                penaltyTreasury
            );
        } else {
            _transferTo(beneficiary, current.stakedAmount);
            stakeReturned = current.stakedAmount;
            settlementStatus = SETTLEMENT_NO_REWARD;
        }

        current.settlementStatus = settlementStatus;
        current.rewardPaid = rewardPaid;
        current.penaltyCollected = penaltyCollected;

        emit SessionSettled(
            sessionId,
            beneficiary,
            outcome,
            settlementStatus,
            stakeReturned,
            rewardPaid,
            penaltyCollected,
            evidenceHash,
            timestamp
        );
    }

    // ======================================================
    // PREVIEW / READ API
    // ======================================================

    function previewSettlement(
        bytes32 sessionId,
        address beneficiary,
        uint8 outcome
    )
        external
        view
        returns (
            uint8 settlementStatus,
            uint256 stakeReturned,
            uint256 rewardPaid,
            uint256 penaltyCollected,
            bool dailyLimitReached,
            bool poolEmpty
        )
    {
        if (sessionId == bytes32(0)) revert InvalidSessionId();
        if (beneficiary == address(0)) revert InvalidAddress();
        _validateOutcome(outcome);

        Session memory current = sessions[sessionId];
        if (current.started && current.beneficiary != beneficiary) {
            revert BeneficiaryMismatch();
        }

        if (outcome == OUTCOME_VALID_NO_VIOLATION) {
            stakeReturned = stakeAmount;
            dailyLimitReached =
                lastRewardDay[beneficiary] == _rewardDay(block.timestamp);
            rewardPaid = current.started
                ? rewardAmountForDuration(block.timestamp - current.startedAt)
                : rewardAmount;
            poolEmpty = rewardPoolBalance < rewardPaid;

            if (dailyLimitReached) {
                settlementStatus = SETTLEMENT_REWARD_DAILY_LIMIT;
            } else if (poolEmpty) {
                settlementStatus = SETTLEMENT_REWARD_POOL_EMPTY;
            } else {
                settlementStatus = SETTLEMENT_REWARDED;
            }
        } else if (outcome == OUTCOME_VIOLATION_CONFIRMED) {
            settlementStatus = SETTLEMENT_PENALTY;
            penaltyCollected = penaltyAmount;
        } else {
            settlementStatus = SETTLEMENT_NO_REWARD;
            stakeReturned = stakeAmount;
        }
    }

    function getSession(bytes32 sessionId)
        external
        view
        returns (
            address beneficiary,
            uint256 startedAt,
            uint256 settledAt,
            uint256 stakedAmount,
            uint256 rewardPaid,
            uint256 penaltyCollected,
            uint8 outcome,
            uint8 settlementStatus,
            bytes32 evidenceHash,
            uint256 outcomeTimestamp,
            bool started,
            bool settled
        )
    {
        Session memory current = sessions[sessionId];
        return (
            current.beneficiary,
            current.startedAt,
            current.settledAt,
            current.stakedAmount,
            current.rewardPaid,
            current.penaltyCollected,
            current.outcome,
            current.settlementStatus,
            current.evidenceHash,
            current.outcomeTimestamp,
            current.started,
            current.settled
        );
    }

    function currentRewardDay() external view returns (uint256) {
        return _rewardDay(block.timestamp);
    }

    /// @notice Returns the reward for an elapsed session duration.
    /// @dev The base reward starts at 30 seconds and reaches a 100% bonus at
    ///      60 seconds. Integer seconds are used because block timestamps are
    ///      integer seconds; the result is still exact to the token decimals.
    function rewardAmountForDuration(uint256 durationSeconds)
        public
        pure
        returns (uint256)
    {
        if (durationSeconds <= MIN_REWARD_DURATION) return rewardAmount;

        uint256 bonusSeconds = durationSeconds - MIN_REWARD_DURATION;
        if (bonusSeconds > FULL_BONUS_DURATION - MIN_REWARD_DURATION) {
            bonusSeconds = FULL_BONUS_DURATION - MIN_REWARD_DURATION;
        }

        return rewardAmount
            + (rewardAmount * MAX_REWARD_BONUS_PERCENT * bonusSeconds)
                / (100 * (FULL_BONUS_DURATION - MIN_REWARD_DURATION));
    }

    function outcomeName(uint8 outcome)
        external
        pure
        returns (string memory)
    {
        _validateOutcome(outcome);

        if (outcome == OUTCOME_VALID_NO_VIOLATION) {
            return "VALID_NO_VIOLATION";
        }
        if (outcome == OUTCOME_VIOLATION_CONFIRMED) {
            return "VIOLATION_CONFIRMED";
        }
        if (outcome == OUTCOME_SENSOR_FAULT) {
            return "SENSOR_FAULT";
        }
        return "INCOMPLETE";
    }

    function _validateOutcome(uint8 outcome) internal pure {
        if (outcome > OUTCOME_INCOMPLETE) revert InvalidOutcome();
    }

    function _rewardDay(uint256 timestamp) internal pure returns (uint256) {
        return timestamp / SECONDS_PER_DAY;
    }

    // ======================================================
    // TOKEN HELPERS
    // ======================================================

    function _transferFrom(address from, address to, uint256 amount) internal {
        bool ok = IHRCRewardToken(token).transferFrom(from, to, amount);
        if (!ok) revert TokenTransferFailed();
    }

    function _transferTo(address to, uint256 amount) internal {
        bool ok = IHRCRewardToken(token).transfer(to, amount);
        if (!ok) revert TokenTransferFailed();
    }
}
