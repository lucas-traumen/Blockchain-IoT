// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

/// @title HRCRewardToken
/// @notice Minimal ERC-20 token with a fixed supply minted once in the constructor.
/// @dev There is intentionally no mint or burn entry point after deployment.
contract HRCRewardToken {
    error InvalidAddress();
    error InvalidMetadata();
    error InsufficientBalance();
    error InsufficientAllowance();

    string public name;
    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public immutable totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(
        string memory tokenName,
        string memory tokenSymbol,
        uint256 initialSupply,
        address initialHolder
    ) {
        if (initialHolder == address(0)) revert InvalidAddress();
        if (bytes(tokenName).length == 0 || bytes(tokenSymbol).length == 0) {
            revert InvalidMetadata();
        }

        name = tokenName;
        symbol = tokenSymbol;
        totalSupply = initialSupply;
        balanceOf[initialHolder] = initialSupply;

        emit Transfer(address(0), initialHolder, initialSupply);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        if (spender == address(0)) revert InvalidAddress();

        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(
        address from,
        address to,
        uint256 amount
    ) external returns (bool) {
        uint256 currentAllowance = allowance[from][msg.sender];
        if (currentAllowance < amount) revert InsufficientAllowance();

        if (currentAllowance != type(uint256).max) {
            unchecked {
                allowance[from][msg.sender] = currentAllowance - amount;
            }
            emit Approval(from, msg.sender, allowance[from][msg.sender]);
        }

        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        if (to == address(0)) revert InvalidAddress();
        if (balanceOf[from] < amount) revert InsufficientBalance();

        unchecked {
            balanceOf[from] -= amount;
            balanceOf[to] += amount;
        }

        emit Transfer(from, to, amount);
    }
}
