// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {Ownable2Step, Ownable} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

interface IHRCRewardToken {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title HRCSessionRewards
/// @notice Session stake, duration-based safety reward and violation penalty.
/// @dev The contract does not mint tokens. It moves fixed-supply ERC-20 tokens
///      between users, this contract's reward pool, and the penalty treasury.
///      Ownership uses OpenZeppelin Ownable2Step, reentrancy is guarded by
///      ReentrancyGuard and settlement can be halted with Pausable.
contract HRCSessionRewards is Ownable2Step, Pausable, ReentrancyGuard {
    // ======================================================
    // SESSION REWARD POLICY
    // ======================================================

    // The project token uses 18 decimals, so 1 coin is 1 ether unit.
    // These are owner-configurable so the policy can be tuned without a
    // redeploy. The names are kept from the constant-only version so the
    // backend read API and existing ABI consumers keep working.
    uint256 public stakeAmount = 2 ether;
    // rewardAmount is the base reward at the MIN_REWARD_DURATION threshold.
    uint256 public rewardAmount = 3 ether;
    uint256 public penaltyAmount = 2 ether;
    uint256 public MIN_REWARD_DURATION = 30 seconds;
    uint256 public FULL_BONUS_DURATION = 60 seconds;
    uint256 public MAX_REWARD_BONUS_PERCENT = 100;
    uint256 public constant SECONDS_PER_DAY = 1 days;
    // Upper bound a single funder can hold in the pool. It bounds the total
    // outstanding reward liability of this contract.
    uint256 public maxRewardPool = 100000 ether;

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

    error NotOperator();
    error InvalidAddress();
    error InvalidAmount();
    error InvalidSessionId();
    error InvalidEvidenceHash();
    error InvalidTimestamp();
    error InvalidSessionEndTimestamp();
    error InvalidOutcome();
    error SessionAlreadyStarted();
    error SessionNotFound();
    error SessionAlreadySettled();
    error BeneficiaryMismatch();
    error CannotRemoveOwner();
    error RewardPoolCapExceeded(uint256 maximum, uint256 requested);
    error InsufficientSurplus(uint256 requested, uint256 available);
    error InvalidPolicy();
    error ActiveStakesPreventPolicyChange();
    error TokenTransferFailed();

    // ======================================================
    // TYPES AND STORAGE
    // ======================================================

    struct Session {
        address beneficiary;
        uint256 startedAt;
        uint256 endedAt;
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

    mapping(address => bool) public operators;

    mapping(bytes32 => Session) public sessions;
    mapping(bytes32 => bool) public hasSettled;
    mapping(address => uint256) public lastRewardDay;

    // Accounting is separate from the token balance because the same contract
    // temporarily holds both reward-pool tokens and active session stakes.
    uint256 public rewardPoolBalance;
    uint256 public escrowedStakeTotal;

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
        uint256 timestamp,
        uint256 endedAt
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
    event PolicyUpdated(
        uint256 stakeAmount,
        uint256 rewardAmount,
        uint256 penaltyAmount,
        uint256 minRewardDuration,
        uint256 fullBonusDuration,
        uint256 maxRewardBonusPercent
    );
    event MaxRewardPoolUpdated(uint256 previousMaximum, uint256 newMaximum);
    event RewardPoolPaused(address indexed operator);
    event RewardPoolUnpaused(address indexed operator);
    event ExcessTokenSwept(address indexed to, uint256 amount);

    // ======================================================
    // MODIFIERS / CONSTRUCTOR
    // ======================================================

    modifier onlyOperator() {
        if (!operators[msg.sender]) revert NotOperator();
        _;
    }

    constructor(address rewardToken, address treasury) Ownable(msg.sender) {
        if (rewardToken == address(0) || treasury == address(0)) {
            revert InvalidAddress();
        }

        token = rewardToken;
        rewardPool = address(this);
        penaltyTreasury = treasury;
        operators[msg.sender] = true;
    }

    /// @dev Keeping the operator flag in sync with ownership keeps the existing
    ///      operator read API authoritative after a two-step transfer.
    function _transferOwnership(address newOwner) internal override {
        operators[newOwner] = true;
        super._transferOwnership(newOwner);
    }

    // ======================================================
    // OWNERSHIP AND OPERATORS
    // ======================================================

    function addOperator(address operator) external onlyOwner {
        if (operator == address(0)) revert InvalidAddress();
        operators[operator] = true;
        emit OperatorAdded(operator);
    }

    function removeOperator(address operator) external onlyOwner {
        if (operator == owner()) revert CannotRemoveOwner();
        operators[operator] = false;
        emit OperatorRemoved(operator);
    }

    // ======================================================
    // POLICY AND EMERGENCY CONTROLS
    // ======================================================

    /// @notice Update policy only when no session stake is escrowed.
    function setPolicy(
        uint256 newStakeAmount,
        uint256 newRewardAmount,
        uint256 newPenaltyAmount,
        uint256 newMinRewardDuration,
        uint256 newFullBonusDuration,
        uint256 newMaxRewardBonusPercent
    ) external onlyOwner {
        if (escrowedStakeTotal > 0) revert ActiveStakesPreventPolicyChange();
        if (
            newStakeAmount == 0 ||
            newRewardAmount == 0 ||
            newPenaltyAmount == 0 ||
            newPenaltyAmount > newStakeAmount ||
            newMinRewardDuration == 0 ||
            newFullBonusDuration <= newMinRewardDuration ||
            newMaxRewardBonusPercent > 1000
        ) revert InvalidPolicy();

        stakeAmount = newStakeAmount;
        rewardAmount = newRewardAmount;
        penaltyAmount = newPenaltyAmount;
        MIN_REWARD_DURATION = newMinRewardDuration;
        FULL_BONUS_DURATION = newFullBonusDuration;
        MAX_REWARD_BONUS_PERCENT = newMaxRewardBonusPercent;

        emit PolicyUpdated(
            newStakeAmount,
            newRewardAmount,
            newPenaltyAmount,
            newMinRewardDuration,
            newFullBonusDuration,
            newMaxRewardBonusPercent
        );
    }

    function setMaxRewardPool(uint256 newMaximum) external onlyOwner {
        if (newMaximum == 0 || newMaximum < rewardPoolBalance) {
            revert InvalidPolicy();
        }

        uint256 previousMaximum = maxRewardPool;
        maxRewardPool = newMaximum;
        emit MaxRewardPoolUpdated(previousMaximum, newMaximum);
    }

    /// @notice Halt new stakes and pool funding. Settlement stays open so users
    ///         can always return a session and recover their stake.
    function pause() external onlyOwner {
        _pause();
        emit RewardPoolPaused(msg.sender);
    }

    function unpause() external onlyOwner {
        _unpause();
        emit RewardPoolUnpaused(msg.sender);
    }

    /// @notice Sweep tokens sent to this contract by mistake. Only the surplus
    ///         above the reward pool and escrowed stake can be moved, so this can
    ///         never touch user collateral or funded rewards.
    function sweepExcessToken(address to, uint256 amount) external onlyOwner nonReentrant whenNotPaused {
        if (to == address(0)) revert InvalidAddress();
        if (amount == 0) revert InvalidAmount();
        uint256 committed = rewardPoolBalance + escrowedStakeTotal;
        uint256 balance = IHRCRewardToken(token).balanceOf(address(this));
        if (balance <= committed || amount > balance - committed) {
            revert InsufficientSurplus(amount, balance > committed ? balance - committed : 0);
        }
        _transferTo(to, amount);
        emit ExcessTokenSwept(to, amount);
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
        whenNotPaused
    {
        if (amount == 0) revert InvalidAmount();
        uint256 requested = rewardPoolBalance + amount;
        if (requested > maxRewardPool) {
            revert RewardPoolCapExceeded(maxRewardPool, requested);
        }
        _transferFrom(msg.sender, rewardPool, amount);

        rewardPoolBalance = requested;
        emit RewardPoolFunded(msg.sender, amount, rewardPoolBalance);
    }

    // ======================================================
    // SESSION STAKE
    // ======================================================

    /// @notice User starts a session and locks exactly stakeAmount() tokens.
    function startSession(bytes32 sessionId) external nonReentrant whenNotPaused {
        _startSession(sessionId, msg.sender);
    }

    /// @notice Operator-assisted start for a beneficiary that approved this contract.
    function startSessionFor(bytes32 sessionId, address beneficiary)
        external
        onlyOperator
        nonReentrant
        whenNotPaused
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
        uint256 timestamp,
        uint256 endedAt
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
            timestamp,
            endedAt
        );
    }

    /// @notice Compatibility alias with the same policy and parameter order.
    function settleSession(
        bytes32 sessionId,
        address beneficiary,
        uint8 outcome,
        bytes32 evidenceHash,
        uint256 timestamp,
        uint256 endedAt
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
            timestamp,
            endedAt
        );
    }

    function _settleSession(
        bytes32 sessionId,
        address beneficiary,
        uint8 outcome,
        bytes32 evidenceHash,
        uint256 timestamp,
        uint256 endedAt
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
        if (endedAt < current.startedAt || endedAt > block.timestamp) {
            revert InvalidSessionEndTimestamp();
        }

        // Mark the session before token calls. nonReentrant protects the whole
        // operation, and a failed token transfer reverts all state changes.
        current.settled = true;
        current.endedAt = endedAt;
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

            uint256 duration = endedAt - current.startedAt;
            if (duration < MIN_REWARD_DURATION) {
                settlementStatus = SETTLEMENT_NO_REWARD;
            } else {
                uint256 rewardDay = _rewardDay(block.timestamp);
                uint256 sessionReward = rewardAmountForDuration(duration);
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
            timestamp,
            endedAt
        );
    }

    // ======================================================
    // PREVIEW / READ API
    // ======================================================

    function previewSettlement(
        bytes32 sessionId,
        address beneficiary,
        uint8 outcome,
        uint256 endedAt
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
        if (endedAt > block.timestamp || (current.started && endedAt < current.startedAt)) {
            revert InvalidSessionEndTimestamp();
        }

        if (outcome == OUTCOME_VALID_NO_VIOLATION) {
            stakeReturned = current.started ? current.stakedAmount : stakeAmount;
            uint256 duration = current.started
                ? endedAt - current.startedAt
                : 0;
            if (duration < MIN_REWARD_DURATION) {
                settlementStatus = SETTLEMENT_NO_REWARD;
            } else {
                dailyLimitReached =
                    lastRewardDay[beneficiary] == _rewardDay(block.timestamp);
                rewardPaid = rewardAmountForDuration(duration);
                poolEmpty = rewardPoolBalance < rewardPaid;

                if (dailyLimitReached) {
                    settlementStatus = SETTLEMENT_REWARD_DAILY_LIMIT;
                } else if (poolEmpty) {
                    settlementStatus = SETTLEMENT_REWARD_POOL_EMPTY;
                } else {
                    settlementStatus = SETTLEMENT_REWARDED;
                }
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

    function getSessionEndTime(bytes32 sessionId) external view returns (uint256) {
        return sessions[sessionId].endedAt;
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
        view
        returns (uint256)
    {
        if (durationSeconds < MIN_REWARD_DURATION) return 0;
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
