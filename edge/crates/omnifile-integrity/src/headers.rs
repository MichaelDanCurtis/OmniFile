//! Wire encodings of a CRC-64/NVME value. Azure and S3 use the same CRC but
//! **opposite byte orders**, so the same checksum has two different strings.
//!
//! * **Azure `x-ms-content-crc64`: Base64 of the 8-byte little-endian value.**
//!   Microsoft's structured-body spec gives `segment-data-crc64` /
//!   `message-data-crc64` as `byte[8]` and states "All integer data types are
//!   encoded as little-endian"; its worked example lists the CRC of the byte
//!   `0x11` as `d0 61 67 57 b4 5f 54 d2`, which is our value in little-endian
//!   order (unit-tested in this crate). The header itself is documented only
//!   as "A CRC64 hash of the block content"; the Azure .NET SDK fills it from
//!   `BitConverter.GetBytes(ulong)` (little-endian) Base64-encoded.
//!   Microsoft's own Put Block sample response shows an unpadded value
//!   (`x-ms-content-crc64: 77uWZTolTHU`), so the parser accepts both padded
//!   and unpadded input; we always emit padded standard Base64.
//!   Sources: <https://learn.microsoft.com/rest/api/storageservices/structured-body-format>,
//!   <https://learn.microsoft.com/rest/api/storageservices/put-block>,
//!   `Azure/azure-sdk-for-net` `sdk/storage/Azure.Storage.Common/src/StorageCrc64HashAlgorithm.cs`.
//!
//! * **S3 `x-amz-checksum-crc64nvme`: Base64 of the 8-byte big-endian value.**
//!   AWS: the trailer "value field … includes a base64 encoding of the
//!   big-endian checksum value for that object"
//!   (<https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html>).
//!   For multipart uploads CRC64NVME is full-object only (no composite type),
//!   which is why the per-block values are combined (see [`crate::crc64::combine`]).

use std::fmt;

use base64::Engine as _;
use base64::alphabet;
use base64::engine::DecodePaddingMode;
use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig, STANDARD};

pub const AZURE_CRC64_HEADER: &str = "x-ms-content-crc64";
pub const S3_CRC64NVME_HEADER: &str = "x-amz-checksum-crc64nvme";

/// Standard alphabet, emits padding, accepts input with or without it.
const LENIENT: GeneralPurpose = GeneralPurpose::new(
    &alphabet::STANDARD,
    GeneralPurposeConfig::new()
        .with_encode_padding(true)
        .with_decode_padding_mode(DecodePaddingMode::Indifferent),
);

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HeaderError {
    /// Not valid Base64.
    Base64(String),
    /// Decoded to the wrong number of bytes (a CRC64 is 8).
    Length(usize),
}

impl fmt::Display for HeaderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HeaderError::Base64(e) => write!(f, "checksum header is not valid Base64: {e}"),
            HeaderError::Length(n) => {
                write!(f, "checksum header decodes to {n} bytes; a CRC64 is 8")
            }
        }
    }
}

impl std::error::Error for HeaderError {}

fn decode8(value: &str) -> Result<[u8; 8], HeaderError> {
    let bytes = LENIENT
        .decode(value.trim())
        .map_err(|e| HeaderError::Base64(e.to_string()))?;
    let n = bytes.len();
    bytes.try_into().map_err(|_| HeaderError::Length(n))
}

/// `x-ms-content-crc64` value: Base64 of the little-endian bytes.
pub fn azure_content_crc64(crc: u64) -> String {
    STANDARD.encode(crc.to_le_bytes())
}

/// Parse an `x-ms-content-crc64` value (padded or unpadded).
pub fn parse_azure_content_crc64(value: &str) -> Result<u64, HeaderError> {
    decode8(value).map(u64::from_le_bytes)
}

/// `x-amz-checksum-crc64nvme` value: Base64 of the big-endian bytes.
pub fn s3_checksum_crc64nvme(crc: u64) -> String {
    STANDARD.encode(crc.to_be_bytes())
}

/// Parse an `x-amz-checksum-crc64nvme` value (also the `ChecksumCRC64NVME`
/// field of S3 responses and Batch Operations reports).
pub fn parse_s3_checksum_crc64nvme(value: &str) -> Result<u64, HeaderError> {
    decode8(value).map(u64::from_be_bytes)
}

/// Translate an Azure CRC64 header value into the S3 form of the same CRC.
pub fn azure_to_s3(value: &str) -> Result<String, HeaderError> {
    parse_azure_content_crc64(value).map(s3_checksum_crc64nvme)
}
