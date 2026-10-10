const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const { anyValue } = require("@nomicfoundation/hardhat-chai-matchers/withArgs");

// ======================================================
// HELPERS
// ======================================================

const HASH = ethers.keccak256(ethers.toUtf8Bytes("sensor-data"));
const ZERO_HASH = ethers.ZeroHash;

const RISK = { SAFE: 0, LOW: 1, WARNING: 2, DANGER: 3 };

const TYPES = {
    SafetyEvent: [
        { name: "eventId", type: "string" },
        { name: "deviceId", type: "string" },
        { name: "timestamp", type: "uint256" },
        { name: "dataHash", type: "bytes32" },
        { name: "riskLevel", type: "uint8" }
    ]
};

async function deployFixture() {
    const [owner, operator, stranger, newOwner] = await ethers.getSigners();

    const Factory = await ethers.getContractFactory("HRCSafetyLog");
    const log = await Factory.deploy();
    await log.waitForDeployment();

    const deviceWallet = ethers.Wallet.createRandom();
    const otherWallet = ethers.Wallet.createRandom();

    const { chainId } = await ethers.provider.getNetwork();
    const domain = {
        name: "HRCSafetyLog",
        version: "1",
        chainId,
        verifyingContract: await log.getAddress()
    };

    return { log, owner, operator, stranger, newOwner, deviceWallet, otherWallet, domain };
}

function makeEvent(overrides = {}) {
    return {
        eventId: "EVT-1",
        deviceId: "ROBOT-ARM-01",
        timestamp: 1790996000,
        dataHash: HASH,
        riskLevel: RISK.SAFE,
        ...overrides
    };
}

async function record(log, signer, ev) {
    return log.connect(signer).recordSafetyEvent(
        ev.eventId, ev.deviceId, ev.timestamp, ev.dataHash, ev.riskLevel
    );
}

async function recordSigned(log, relayer, ev, signature) {
    return log.connect(relayer).recordSafetyEventSigned(
        ev.eventId, ev.deviceId, ev.timestamp, ev.dataHash, ev.riskLevel, signature
    );
}

// ======================================================
// OWNERSHIP & OPERATORS
// ======================================================

describe("Ownership", function () {

    it("deployer is owner and operator", async function () {
        const { log, owner } = await loadFixture(deployFixture);
        expect(await log.owner()).to.equal(owner.address);
        expect(await log.operators(owner.address)).to.equal(true);
    });

    it("two-step transfer: new owner must accept", async function () {
        const { log, owner, newOwner } = await loadFixture(deployFixture);

        await expect(log.transferOwnership(newOwner.address))
            .to.emit(log, "OwnershipTransferStarted")
            .withArgs(owner.address, newOwner.address);

        // chưa accept thì owner vẫn là người cũ
        expect(await log.owner()).to.equal(owner.address);

        await expect(log.connect(newOwner).acceptOwnership())
            .to.emit(log, "OwnershipTransferred")
            .withArgs(owner.address, newOwner.address);

        expect(await log.owner()).to.equal(newOwner.address);
        expect(await log.pendingOwner()).to.equal(ethers.ZeroAddress);
        expect(await log.operators(newOwner.address)).to.equal(true);
    });

    it("old owner loses owner rights after transfer", async function () {
        const { log, owner, newOwner, operator } = await loadFixture(deployFixture);

        await log.transferOwnership(newOwner.address);
        await log.connect(newOwner).acceptOwnership();

        await expect(log.connect(owner).addOperator(operator.address))
            .to.be.revertedWithCustomError(log, "NotOwner");
    });

    it("only owner can start transfer; only pending owner can accept", async function () {
        const { log, stranger, newOwner } = await loadFixture(deployFixture);

        await expect(log.connect(stranger).transferOwnership(newOwner.address))
            .to.be.revertedWithCustomError(log, "NotOwner");

        await log.transferOwnership(newOwner.address);

        await expect(log.connect(stranger).acceptOwnership())
            .to.be.revertedWithCustomError(log, "NotPendingOwner");
    });

    it("rejects zero address as new owner", async function () {
        const { log } = await loadFixture(deployFixture);
        await expect(log.transferOwnership(ethers.ZeroAddress))
            .to.be.revertedWithCustomError(log, "InvalidAddress");
    });
});

describe("Operators", function () {

    it("owner adds operator and event is emitted", async function () {
        const { log, operator } = await loadFixture(deployFixture);

        await expect(log.addOperator(operator.address))
            .to.emit(log, "OperatorAdded")
            .withArgs(operator.address);

        expect(await log.operators(operator.address)).to.equal(true);
    });

    it("rejects zero address operator", async function () {
        const { log } = await loadFixture(deployFixture);
        await expect(log.addOperator(ethers.ZeroAddress))
            .to.be.revertedWithCustomError(log, "InvalidAddress");
    });

    it("non-owner cannot add or remove operator", async function () {
        const { log, stranger, operator } = await loadFixture(deployFixture);

        await expect(log.connect(stranger).addOperator(operator.address))
            .to.be.revertedWithCustomError(log, "NotOwner");

        await expect(log.connect(stranger).removeOperator(operator.address))
            .to.be.revertedWithCustomError(log, "NotOwner");
    });

    it("owner removes operator; removed operator cannot record", async function () {
        const { log, operator } = await loadFixture(deployFixture);

        await log.addOperator(operator.address);

        await expect(log.removeOperator(operator.address))
            .to.emit(log, "OperatorRemoved")
            .withArgs(operator.address);

        await expect(record(log, operator, makeEvent()))
            .to.be.revertedWithCustomError(log, "NotOperator");
    });

    it("cannot remove the owner from operators", async function () {
        const { log, owner } = await loadFixture(deployFixture);
        await expect(log.removeOperator(owner.address))
            .to.be.revertedWithCustomError(log, "CannotRemoveOwner");
    });
});

// ======================================================
// RECORDING
// ======================================================

describe("Recording safety events", function () {

    it("stores all fields and emits SafetyEventRecorded", async function () {
        const { log } = await loadFixture(deployFixture);
        const ev = makeEvent({ riskLevel: RISK.LOW });

        const tx = await record(log, (await ethers.getSigners())[0], ev);

        await expect(tx)
            .to.emit(log, "SafetyEventRecorded")
            .withArgs(ev.eventId, ev.deviceId, ev.timestamp, ev.dataHash, ev.riskLevel);

        const block = await ethers.provider.getBlock((await tx.wait()).blockNumber);
        const stored = await log.getSafetyEvent(ev.eventId);

        expect(stored.eventId).to.equal(ev.eventId);
        expect(stored.deviceId).to.equal(ev.deviceId);
        expect(stored.timestamp).to.equal(ev.timestamp);
        expect(stored.dataHash).to.equal(ev.dataHash);
        expect(stored.riskLevel).to.equal(ev.riskLevel);
        // recordedAt lấy từ block, không phải do client khai
        expect(stored.recordedAt).to.equal(block.timestamp);
    });

    it("increases count and supports index lookup", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent({ eventId: "A" }));
        await record(log, owner, makeEvent({ eventId: "B" }));

        expect(await log.getEventCount()).to.equal(2);
        expect(await log.getEventIdByIndex(0)).to.equal("A");
        expect(await log.getEventIdByIndex(1)).to.equal("B");

        await expect(log.getEventIdByIndex(2))
            .to.be.revertedWithCustomError(log, "IndexOutOfRange");
    });

    it("tracks per-device event count and last event time", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent({ eventId: "A", deviceId: "R1" }));
        await record(log, owner, makeEvent({ eventId: "B", deviceId: "R1" }));
        const tx = await record(log, owner, makeEvent({ eventId: "C", deviceId: "R2" }));

        expect(await log.deviceEventCount("R1")).to.equal(2);
        expect(await log.deviceEventCount("R2")).to.equal(1);

        const block = await ethers.provider.getBlock((await tx.wait()).blockNumber);
        expect(await log.lastEventAt("R2")).to.equal(block.timestamp);
    });

    it("rejects duplicate event ID", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent());

        await expect(record(log, owner, makeEvent()))
            .to.be.revertedWithCustomError(log, "EventAlreadyExists");
    });

    it("rejects invalid risk level", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await expect(record(log, owner, makeEvent({ riskLevel: 4 })))
            .to.be.revertedWithCustomError(log, "InvalidRiskLevel");
    });

    it("rejects empty eventId, deviceId and zero hash", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await expect(record(log, owner, makeEvent({ eventId: "" })))
            .to.be.revertedWithCustomError(log, "EmptyField");

        await expect(record(log, owner, makeEvent({ deviceId: "" })))
            .to.be.revertedWithCustomError(log, "EmptyField");

        await expect(record(log, owner, makeEvent({ dataHash: ZERO_HASH })))
            .to.be.revertedWithCustomError(log, "EmptyField");
    });

    it("unauthorized wallet cannot record", async function () {
        const { log, stranger } = await loadFixture(deployFixture);

        await expect(record(log, stranger, makeEvent()))
            .to.be.revertedWithCustomError(log, "NotOperator");
    });

    it("operator can record", async function () {
        const { log, operator } = await loadFixture(deployFixture);

        await log.addOperator(operator.address);
        await record(log, operator, makeEvent());

        expect(await log.getEventCount()).to.equal(1);
    });

    it("reading a missing event reverts", async function () {
        const { log } = await loadFixture(deployFixture);

        await expect(log.getSafetyEvent("NOPE"))
            .to.be.revertedWithCustomError(log, "EventNotFound");
    });
});

// ======================================================
// AUTOMATIC EMERGENCY STOP
// ======================================================

describe("Automatic Emergency Stop", function () {

    it("DANGER locks the robot in the same transaction", async function () {
        const { log, owner } = await loadFixture(deployFixture);
        const ev = makeEvent({ riskLevel: RISK.DANGER });

        const tx = await record(log, owner, ev);

        await expect(tx).to.emit(log, "SafetyEventRecorded");
        await expect(tx)
            .to.emit(log, "EmergencyStopTriggered")
            .withArgs(ev.deviceId, ev.eventId, anyValue, owner.address);

        expect(await log.isRobotLocked(ev.deviceId)).to.equal(true);
        expect(await log.canOperate(ev.deviceId)).to.equal(false);
    });

    it("SAFE, LOW and WARNING do not lock the robot", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        for (const level of [RISK.SAFE, RISK.LOW, RISK.WARNING]) {
            const tx = await record(log, owner, makeEvent({
                eventId: `EVT-${level}`,
                riskLevel: level
            }));
            await expect(tx).to.not.emit(log, "EmergencyStopTriggered");
        }

        expect(await log.isRobotLocked("ROBOT-ARM-01")).to.equal(false);
        expect(await log.canOperate("ROBOT-ARM-01")).to.equal(true);
    });

    it("stores lock info (which event, who, when)", async function () {
        const { log, owner } = await loadFixture(deployFixture);
        const ev = makeEvent({ riskLevel: RISK.DANGER });

        const tx = await record(log, owner, ev);
        const block = await ethers.provider.getBlock((await tx.wait()).blockNumber);

        const info = await log.getLockInfo(ev.deviceId);

        expect(info.locked).to.equal(true);
        expect(info.eventId).to.equal(ev.eventId);
        expect(info.lockedBy).to.equal(owner.address);
        expect(info.lockedAt).to.equal(block.timestamp);
    });

    it("second DANGER on a locked robot is recorded without a second stop", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent({ eventId: "D1", riskLevel: RISK.DANGER }));

        const tx = await record(log, owner, makeEvent({ eventId: "D2", riskLevel: RISK.DANGER }));

        await expect(tx).to.emit(log, "SafetyEventRecorded");
        await expect(tx).to.not.emit(log, "EmergencyStopTriggered");

        expect(await log.getEventCount()).to.equal(2);
        // lock info vẫn trỏ về event đầu tiên
        expect((await log.getLockInfo("ROBOT-ARM-01")).eventId).to.equal("D1");
    });

    it("locks are independent per device", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent({
            eventId: "D1", deviceId: "R1", riskLevel: RISK.DANGER
        }));

        expect(await log.isRobotLocked("R1")).to.equal(true);
        expect(await log.isRobotLocked("R2")).to.equal(false);
    });
});

// ======================================================
// MANUAL EMERGENCY STOP & UNLOCK
// ======================================================

describe("Manual Emergency Stop and Unlock", function () {

    it("operator can escalate a WARNING event", async function () {
        const { log, owner } = await loadFixture(deployFixture);
        const ev = makeEvent({ riskLevel: RISK.WARNING });

        await record(log, owner, ev);

        await expect(log.triggerEmergencyStop(ev.deviceId, ev.eventId))
            .to.emit(log, "EmergencyStopTriggered")
            .withArgs(ev.deviceId, ev.eventId, anyValue, owner.address);

        expect(await log.isRobotLocked(ev.deviceId)).to.equal(true);
    });

    it("rejects unknown event", async function () {
        const { log } = await loadFixture(deployFixture);

        await expect(log.triggerEmergencyStop("ROBOT-ARM-01", "NOPE"))
            .to.be.revertedWithCustomError(log, "EventNotFound");
    });

    it("rejects event that belongs to another robot", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent({
            eventId: "E1", deviceId: "R1", riskLevel: RISK.WARNING
        }));

        await expect(log.triggerEmergencyStop("R2", "E1"))
            .to.be.revertedWithCustomError(log, "EventDeviceMismatch");
    });

    it("rejects events below WARNING", async function () {
        const { log, owner } = await loadFixture(deployFixture);
        const ev = makeEvent({ riskLevel: RISK.LOW });

        await record(log, owner, ev);

        await expect(log.triggerEmergencyStop(ev.deviceId, ev.eventId))
            .to.be.revertedWithCustomError(log, "RiskTooLow");
    });

    it("rejects when robot is already locked", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent({ eventId: "D1", riskLevel: RISK.DANGER }));
        await record(log, owner, makeEvent({ eventId: "W1", riskLevel: RISK.WARNING }));

        await expect(log.triggerEmergencyStop("ROBOT-ARM-01", "W1"))
            .to.be.revertedWithCustomError(log, "RobotAlreadyLocked");
    });

    it("non-operator cannot trigger emergency stop", async function () {
        const { log, owner, stranger } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent({ riskLevel: RISK.WARNING }));

        await expect(log.connect(stranger).triggerEmergencyStop("ROBOT-ARM-01", "EVT-1"))
            .to.be.revertedWithCustomError(log, "NotOperator");
    });

    it("owner unlocks a locked robot", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent({ riskLevel: RISK.DANGER }));

        await expect(log.unlockRobot("ROBOT-ARM-01"))
            .to.emit(log, "RobotUnlocked")
            .withArgs("ROBOT-ARM-01", anyValue, owner.address);

        expect(await log.isRobotLocked("ROBOT-ARM-01")).to.equal(false);
        expect(await log.canOperate("ROBOT-ARM-01")).to.equal(true);
    });

    it("unlocking a robot that is not locked reverts", async function () {
        const { log } = await loadFixture(deployFixture);

        await expect(log.unlockRobot("ROBOT-ARM-01"))
            .to.be.revertedWithCustomError(log, "RobotNotLocked");
    });

    it("only owner can unlock (operator cannot)", async function () {
        const { log, owner, operator } = await loadFixture(deployFixture);

        await log.addOperator(operator.address);
        await record(log, owner, makeEvent({ riskLevel: RISK.DANGER }));

        await expect(log.connect(operator).unlockRobot("ROBOT-ARM-01"))
            .to.be.revertedWithCustomError(log, "NotOwner");
    });

    it("robot can be locked again after unlock", async function () {
        const { log, owner } = await loadFixture(deployFixture);

        await record(log, owner, makeEvent({ eventId: "D1", riskLevel: RISK.DANGER }));
        await log.unlockRobot("ROBOT-ARM-01");

        await record(log, owner, makeEvent({ eventId: "D2", riskLevel: RISK.DANGER }));

        expect(await log.isRobotLocked("ROBOT-ARM-01")).to.equal(true);
        expect((await log.getLockInfo("ROBOT-ARM-01")).eventId).to.equal("D2");
    });
});

// ======================================================
// DEVICE IDENTITY (EIP-712)
// ======================================================

describe("Device identity and signatures", function () {

    async function registered() {
        const base = await loadFixture(deployFixture);
        await base.log.registerDevice("ROBOT-ARM-01", base.deviceWallet.address);
        return base;
    }

    it("owner registers a device", async function () {
        const { log, deviceWallet } = await loadFixture(deployFixture);

        await expect(log.registerDevice("ROBOT-ARM-01", deviceWallet.address))
            .to.emit(log, "DeviceRegistered")
            .withArgs("ROBOT-ARM-01", deviceWallet.address);

        const d = await log.getDevice("ROBOT-ARM-01");
        expect(d.signer).to.equal(deviceWallet.address);
        expect(d.revoked).to.equal(false);
    });

    it("register rejects non-owner, duplicate, empty id, zero address", async function () {
        const { log, stranger, deviceWallet } = await loadFixture(deployFixture);

        await expect(log.connect(stranger).registerDevice("R1", deviceWallet.address))
            .to.be.revertedWithCustomError(log, "NotOwner");

        await expect(log.registerDevice("", deviceWallet.address))
            .to.be.revertedWithCustomError(log, "EmptyField");

        await expect(log.registerDevice("R1", ethers.ZeroAddress))
            .to.be.revertedWithCustomError(log, "InvalidAddress");

        await log.registerDevice("R1", deviceWallet.address);

        await expect(log.registerDevice("R1", deviceWallet.address))
            .to.be.revertedWithCustomError(log, "DeviceAlreadyRegistered");
    });

    it("unsigned recording is refused for a registered device", async function () {
        const { log, owner } = await registered();

        await expect(record(log, owner, makeEvent()))
            .to.be.revertedWithCustomError(log, "SignatureRequired");
    });

    it("on-chain EIP-712 digest matches ethers TypedDataEncoder", async function () {
        const { log, domain } = await registered();
        const ev = makeEvent();

        const onChain = await log.hashSafetyEvent(
            ev.eventId, ev.deviceId, ev.timestamp, ev.dataHash, ev.riskLevel
        );

        expect(onChain).to.equal(ethers.TypedDataEncoder.hash(domain, TYPES, ev));
    });

    it("accepts a valid device signature relayed by an operator", async function () {
        const { log, owner, deviceWallet, domain } = await registered();
        const ev = makeEvent({ riskLevel: RISK.LOW });

        const sig = await deviceWallet.signTypedData(domain, TYPES, ev);

        await expect(recordSigned(log, owner, ev, sig))
            .to.emit(log, "SafetyEventRecorded")
            .withArgs(ev.eventId, ev.deviceId, ev.timestamp, ev.dataHash, ev.riskLevel);

        expect(await log.getEventCount()).to.equal(1);
    });

    it("signed DANGER event also triggers automatic stop", async function () {
        const { log, owner, deviceWallet, domain } = await registered();
        const ev = makeEvent({ riskLevel: RISK.DANGER });

        const sig = await deviceWallet.signTypedData(domain, TYPES, ev);

        await expect(recordSigned(log, owner, ev, sig))
            .to.emit(log, "EmergencyStopTriggered");

        expect(await log.isRobotLocked(ev.deviceId)).to.equal(true);
    });

    it("rejects a signature from the wrong key", async function () {
        const { log, owner, otherWallet, domain } = await registered();
        const ev = makeEvent();

        const sig = await otherWallet.signTypedData(domain, TYPES, ev);

        await expect(recordSigned(log, owner, ev, sig))
            .to.be.revertedWithCustomError(log, "InvalidSignature");
    });

    it("rejects tampered data (operator cannot change risk level)", async function () {
        const { log, owner, deviceWallet, domain } = await registered();
        const signed = makeEvent({ riskLevel: RISK.DANGER });

        const sig = await deviceWallet.signTypedData(domain, TYPES, signed);

        // operator cố hạ DANGER xuống SAFE để che giấu sự cố
        const tampered = { ...signed, riskLevel: RISK.SAFE };

        await expect(recordSigned(log, owner, tampered, sig))
            .to.be.revertedWithCustomError(log, "InvalidSignature");
    });

    it("rejects replaying the same signed event", async function () {
        const { log, owner, deviceWallet, domain } = await registered();
        const ev = makeEvent();

        const sig = await deviceWallet.signTypedData(domain, TYPES, ev);

        await recordSigned(log, owner, ev, sig);

        await expect(recordSigned(log, owner, ev, sig))
            .to.be.revertedWithCustomError(log, "EventAlreadyExists");
    });

    it("rejects signature bound to a different contract/chain domain", async function () {
        const { log, owner, deviceWallet, domain } = await registered();
        const ev = makeEvent();

        const wrongDomain = { ...domain, verifyingContract: ethers.Wallet.createRandom().address };
        const sig = await deviceWallet.signTypedData(wrongDomain, TYPES, ev);

        await expect(recordSigned(log, owner, ev, sig))
            .to.be.revertedWithCustomError(log, "InvalidSignature");
    });

    it("rejects malleable (high-s) signatures", async function () {
        const { log, owner, deviceWallet, domain } = await registered();
        const ev = makeEvent();

        const good = ethers.Signature.from(await deviceWallet.signTypedData(domain, TYPES, ev));

        const N = BigInt("0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141");
        const highS = N - BigInt(good.s);
        const flippedV = good.v === 27 ? 28 : 27;

        const malleable = ethers.concat([
            good.r,
            ethers.toBeHex(highS, 32),
            ethers.toBeHex(flippedV, 1)
        ]);

        await expect(recordSigned(log, owner, ev, malleable))
            .to.be.revertedWithCustomError(log, "InvalidSignature");
    });

    it("rejects signature with wrong length", async function () {
        const { log, owner } = await registered();

        await expect(recordSigned(log, owner, makeEvent(), "0x1234"))
            .to.be.revertedWithCustomError(log, "InvalidSignature");
    });

    it("signed recording for an unregistered device is rejected", async function () {
        const { log, owner, deviceWallet, domain } = await loadFixture(deployFixture);
        const ev = makeEvent();

        const sig = await deviceWallet.signTypedData(domain, TYPES, ev);

        await expect(recordSigned(log, owner, ev, sig))
            .to.be.revertedWithCustomError(log, "DeviceNotRegistered");
    });

    it("revoked device can no longer submit events", async function () {
        const { log, owner, deviceWallet, domain } = await registered();

        await expect(log.revokeDevice("ROBOT-ARM-01"))
            .to.emit(log, "DeviceRevoked")
            .withArgs("ROBOT-ARM-01", anyValue);

        const ev = makeEvent();
        const sig = await deviceWallet.signTypedData(domain, TYPES, ev);

        await expect(recordSigned(log, owner, ev, sig))
            .to.be.revertedWithCustomError(log, "DeviceIsRevoked");

        expect(await log.canOperate("ROBOT-ARM-01")).to.equal(false);
    });

    it("revoke rejects non-owner and unknown device", async function () {
        const { log, stranger } = await registered();

        await expect(log.connect(stranger).revokeDevice("ROBOT-ARM-01"))
            .to.be.revertedWithCustomError(log, "NotOwner");

        await expect(log.revokeDevice("UNKNOWN"))
            .to.be.revertedWithCustomError(log, "DeviceNotRegistered");
    });
});
