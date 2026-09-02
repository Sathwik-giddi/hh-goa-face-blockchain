// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title FaceAnchor — trustless on-chain registry of face-scan fingerprints.
/// @notice Anyone can verify a fingerprint on Polygonscan via `verify(bytes32)`
///         without trusting our server, mirror files, or indexer.
contract FaceAnchor {
    mapping(bytes32 => uint64) public anchoredAt;
    mapping(bytes32 => address) public anchoredBy;
    mapping(bytes32 => string) public evidenceCid;

    uint64 public totalAnchored;

    event Anchored(bytes32 indexed fingerprint, address indexed by, uint64 timestamp, string cid);

    /// @notice Anchor a 32-byte fingerprint (SHA-256 of the post + face crop).
    function anchor(bytes32 fingerprint, string calldata cid) external {
        require(anchoredAt[fingerprint] == 0, "already anchored");
        anchoredAt[fingerprint] = uint64(block.timestamp);
        anchoredBy[fingerprint] = msg.sender;
        evidenceCid[fingerprint] = cid;
        unchecked { totalAnchored += 1; }
        emit Anchored(fingerprint, msg.sender, uint64(block.timestamp), cid);
    }

    /// @notice Trustless verification: exists, when, who, and evidence pointer.
    /// @return exists true if this fingerprint was ever anchored
    /// @return timestamp block time of the first anchor
    /// @return by address that anchored it
    /// @return cid off-chain evidence pointer (ipfs/https)
    function verify(bytes32 fingerprint)
        external view
        returns (bool exists, uint64 timestamp, address by, string memory cid)
    {
        uint64 ts = anchoredAt[fingerprint];
        return (ts != 0, ts, anchoredBy[fingerprint], evidenceCid[fingerprint]);
    }
}
