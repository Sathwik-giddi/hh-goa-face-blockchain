// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Anchor {
    mapping(bytes32 => uint64) public anchoredAt;
    mapping(bytes32 => string) public cidOf;
    event Anchored(bytes32 indexed hash, address indexed author, uint64 timestamp, string cid);

    function anchor(bytes32 hash_, string calldata cid) external {
        require(anchoredAt[hash_] == 0, "already");
        anchoredAt[hash_] = uint64(block.timestamp);
        cidOf[hash_] = cid;
        emit Anchored(hash_, msg.sender, uint64(block.timestamp), cid);
    }

    function verify(bytes32 hash_) external view returns (bool exists, uint64 ts, string memory cid) {
        ts = anchoredAt[hash_];
        exists = ts != 0;
        cid = cidOf[hash_];
    }
}
