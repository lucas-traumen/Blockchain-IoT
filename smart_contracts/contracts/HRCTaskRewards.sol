// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {Ownable2Step, Ownable} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

interface IHRCTaskToken {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title HRCTaskRewards
/// @notice V2 task-bound escrow. V1 HRCSessionRewards remains deployable and unchanged.
contract HRCTaskRewards is Ownable2Step, Pausable, ReentrancyGuard {
    uint256 public constant SECONDS_PER_DAY = 1 days;
    uint256 public constant MIN_SAFE_DURATION = 120 seconds;
    uint256 public constant STAKE_AMOUNT = 0.200 ether;
    uint256 public constant SAFE_REWARD = 0.002 ether;
    uint256 public constant VIOLATION_PENALTY = 0.010 ether;
    uint256 public constant DAILY_REWARD_CAP = 0.010 ether;

    uint8 public constant COMPLETED_SAFE = 0;
    uint8 public constant VIOLATION_CONFIRMED = 1;
    uint8 public constant SENSOR_FAULT = 2;
    uint8 public constant INCOMPLETE = 3;
    uint8 public constant OWNER_CANCELLED = 4;
    uint8 public constant SYSTEM_CANCELLED = 5;

    error InvalidAddress();
    error InvalidIdentifier();
    error InvalidEvidence();
    error InvalidOutcome();
    error SessionExists();
    error SessionNotFound();
    error SessionAlreadySettled();
    error BeneficiaryMismatch();
    error InvalidEndTime();
    error SafeDurationTooShort();
    error TransferFailed();
    error InvalidAmount();
    error PoolCapExceeded();
    error InsufficientSurplus();
    error OwnerCannotBeRemoved();

    struct Session {
        address beneficiary;
        bytes32 taskIdHash;
        bytes32 policyHash;
        bytes32 evidenceHash;
        uint64 startedAt;
        uint64 endedAt;
        uint64 settledAt;
        uint128 stakedAmount;
        uint128 rewardPaid;
        uint128 penaltyCollected;
        uint8 outcome;
        bool started;
        bool settled;
    }

    address public immutable token;
    address public penaltyTreasury;
    uint256 public maxRewardPool = 100_000 ether;
    uint256 public rewardPoolBalance;
    uint256 public escrowedStakeTotal;
    mapping(address => bool) public operators;
    mapping(bytes32 => Session) public sessions;
    mapping(address => mapping(uint256 => uint256)) public rewardsPaidByDay;

    event OperatorAdded(address indexed operator);
    event OperatorRemoved(address indexed operator);
    event PenaltyTreasuryUpdated(address indexed previousTreasury, address indexed newTreasury);
    event RewardPoolFunded(address indexed funder, uint256 amount, uint256 newBalance);
    event RewardPoolPaused(address indexed operator);
    event RewardPoolUnpaused(address indexed operator);
    event SessionStarted(bytes32 indexed sessionId, bytes32 indexed taskIdHash, address indexed beneficiary,
        bytes32 policyHash, uint256 stakeAmount, uint256 startedAt);
    event SessionSettled(bytes32 indexed sessionId, bytes32 indexed taskIdHash, address indexed beneficiary,
        bytes32 policyHash, uint8 outcome, uint256 stakeReturned, uint256 rewardPaid,
        uint256 penaltyCollected, bytes32 evidenceHash, uint256 startedAt, uint256 endedAt, uint256 settledAt);

    modifier onlyOperator() {
        if (!operators[msg.sender]) revert InvalidAddress();
        _;
    }

    constructor(address tokenAddress, address treasury) Ownable(msg.sender) {
        if (tokenAddress == address(0) || treasury == address(0)) revert InvalidAddress();
        token = tokenAddress;
        penaltyTreasury = treasury;
        operators[msg.sender] = true;
    }

    function _transferOwnership(address newOwner) internal override {
        operators[newOwner] = true;
        super._transferOwnership(newOwner);
    }

    function addOperator(address operator) external onlyOwner {
        if (operator == address(0)) revert InvalidAddress();
        operators[operator] = true;
        emit OperatorAdded(operator);
    }

    function removeOperator(address operator) external onlyOwner {
        if (operator == owner()) revert OwnerCannotBeRemoved();
        operators[operator] = false;
        emit OperatorRemoved(operator);
    }

    function setPenaltyTreasury(address treasury) external onlyOwner {
        if (treasury == address(0)) revert InvalidAddress();
        address previous = penaltyTreasury;
        penaltyTreasury = treasury;
        emit PenaltyTreasuryUpdated(previous, treasury);
    }

    function setMaxRewardPool(uint256 maximum) external onlyOwner {
        if (maximum == 0 || maximum < rewardPoolBalance) revert InvalidAmount();
        maxRewardPool = maximum;
    }

    function pause() external onlyOwner {
        _pause();
        emit RewardPoolPaused(msg.sender);
    }

    function unpause() external onlyOwner {
        _unpause();
        emit RewardPoolUnpaused(msg.sender);
    }

    function fundRewardPool(uint256 amount) external onlyOwner nonReentrant whenNotPaused {
        if (amount == 0) revert InvalidAmount();
        uint256 nextBalance = rewardPoolBalance + amount;
        if (nextBalance > maxRewardPool) revert PoolCapExceeded();
        _transferFrom(msg.sender, address(this), amount);
        rewardPoolBalance = nextBalance;
        emit RewardPoolFunded(msg.sender, amount, nextBalance);
    }

    function startSession(bytes32 sessionId, bytes32 taskIdHash, bytes32 policyHash)
        external nonReentrant whenNotPaused
    {
        if (sessionId == bytes32(0) || taskIdHash == bytes32(0) || policyHash == bytes32(0)) revert InvalidIdentifier();
        if (sessions[sessionId].started) revert SessionExists();
        _transferFrom(msg.sender, address(this), STAKE_AMOUNT);
        sessions[sessionId] = Session({
            beneficiary: msg.sender,
            taskIdHash: taskIdHash,
            policyHash: policyHash,
            evidenceHash: bytes32(0),
            startedAt: uint64(block.timestamp),
            endedAt: 0,
            settledAt: 0,
            stakedAmount: uint128(STAKE_AMOUNT),
            rewardPaid: 0,
            penaltyCollected: 0,
            outcome: type(uint8).max,
            started: true,
            settled: false
        });
        escrowedStakeTotal += STAKE_AMOUNT;
        emit SessionStarted(sessionId, taskIdHash, msg.sender, policyHash, STAKE_AMOUNT, block.timestamp);
    }

    function submitOutcome(bytes32 sessionId, address beneficiary, uint8 outcome, bytes32 evidenceHash, uint256 endedAt)
        external onlyOperator nonReentrant returns (uint256 stakeReturned, uint256 rewardPaid, uint256 penaltyCollected)
    {
        if (evidenceHash == bytes32(0)) revert InvalidEvidence();
        if (outcome > SYSTEM_CANCELLED) revert InvalidOutcome();
        Session storage session = sessions[sessionId];
        if (!session.started) revert SessionNotFound();
        if (session.settled) revert SessionAlreadySettled();
        if (session.beneficiary != beneficiary) revert BeneficiaryMismatch();
        if (endedAt < session.startedAt || endedAt > block.timestamp || endedAt > type(uint64).max) revert InvalidEndTime();
        if (outcome == COMPLETED_SAFE && endedAt - session.startedAt < MIN_SAFE_DURATION) revert SafeDurationTooShort();

        session.settled = true;
        session.endedAt = uint64(endedAt);
        session.settledAt = uint64(block.timestamp);
        session.outcome = outcome;
        session.evidenceHash = evidenceHash;
        escrowedStakeTotal -= session.stakedAmount;

        if (outcome == COMPLETED_SAFE) {
            stakeReturned = session.stakedAmount;
            _transfer(beneficiary, stakeReturned);
            uint256 day = block.timestamp / SECONDS_PER_DAY;
            uint256 alreadyPaid = rewardsPaidByDay[beneficiary][day];
            if (alreadyPaid + SAFE_REWARD <= DAILY_REWARD_CAP && rewardPoolBalance >= SAFE_REWARD) {
                rewardsPaidByDay[beneficiary][day] = alreadyPaid + SAFE_REWARD;
                rewardPoolBalance -= SAFE_REWARD;
                rewardPaid = SAFE_REWARD;
                _transfer(beneficiary, rewardPaid);
            }
        } else if (outcome == VIOLATION_CONFIRMED) {
            penaltyCollected = VIOLATION_PENALTY;
            stakeReturned = session.stakedAmount - VIOLATION_PENALTY;
            _transfer(beneficiary, stakeReturned);
            _transfer(penaltyTreasury, penaltyCollected);
        } else {
            stakeReturned = session.stakedAmount;
            _transfer(beneficiary, stakeReturned);
        }

        session.rewardPaid = uint128(rewardPaid);
        session.penaltyCollected = uint128(penaltyCollected);
        emit SessionSettled(sessionId, session.taskIdHash, beneficiary, session.policyHash, outcome,
            stakeReturned, rewardPaid, penaltyCollected, evidenceHash, session.startedAt, endedAt, block.timestamp);
    }

    function sweepExcessToken(address to, uint256 amount) external onlyOwner nonReentrant whenNotPaused {
        if (to == address(0) || amount == 0) revert InvalidAmount();
        uint256 committed = rewardPoolBalance + escrowedStakeTotal;
        uint256 balance = IHRCTaskToken(token).balanceOf(address(this));
        if (balance < committed || amount > balance - committed) revert InsufficientSurplus();
        _transfer(to, amount);
    }

    function accountingInvariant() external view returns (bool) {
        return IHRCTaskToken(token).balanceOf(address(this)) >= rewardPoolBalance + escrowedStakeTotal;
    }

    function _transferFrom(address from, address to, uint256 amount) internal {
        if (!IHRCTaskToken(token).transferFrom(from, to, amount)) revert TransferFailed();
    }

    function _transfer(address to, uint256 amount) internal {
        if (!IHRCTaskToken(token).transfer(to, amount)) revert TransferFailed();
    }
}
