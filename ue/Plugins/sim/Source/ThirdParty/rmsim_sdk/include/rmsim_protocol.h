#pragma once

#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace RMSimSdk
{
	constexpr std::uint32_t Magic = 0x524D5344; // RMSD
	constexpr std::uint16_t Version = 1;
	constexpr std::uint16_t ImageTransportVersion = 2;
	constexpr std::uint32_t ImageWidth = 1440;
	constexpr std::uint32_t ImageHeight = 1080;
	constexpr std::uint32_t ImageBytes = ImageWidth * ImageHeight * 4;
	constexpr std::uint32_t ImuCapacity = 512;
	constexpr char ImageSharedMemoryName[] = "/rmsim_camera_v2";
	constexpr char ImuSharedMemoryName[] = "/rmsim_imu_v1";
	static_assert(ImageSharedMemoryName[0] == '/' && ImuSharedMemoryName[0] == '/',
		"POSIX shared-memory names must start with a slash");

	enum class EMessageType : std::uint16_t
	{
		Camera = 1,
		Imu = 2,
		Command = 3,
	};

	enum class EPixelFormat : std::uint32_t
	{
		BGRA8 = 1,
	};

	#pragma pack(push, 1)
	struct FMessageHeader
	{
		std::uint32_t Magic = RMSimSdk::Magic;
		std::uint16_t Version = RMSimSdk::Version;
		std::uint16_t Type = 0;
		std::uint32_t Size = 0;
		std::uint64_t Sequence = 0;
		std::uint64_t TimestampNs = 0;
	};

	struct FImuSample
	{
		FMessageHeader Header;
		float AccelForward = 0;
		float AccelLeft = 0;
		float AccelUp = 0;
		float GyroRoll = 0;
		float GyroPitch = 0;
		float GyroYaw = 0;
		float Yaw = 0;
		float Pitch = 0;
		float Roll = 0;
	};

	struct FImageFrame
	{
		FMessageHeader Header;
		std::uint32_t Width = ImageWidth;
		std::uint32_t Height = ImageHeight;
		std::uint32_t Stride = ImageWidth * 4;
		std::uint32_t DataBytes = 0;
		EPixelFormat PixelFormat = EPixelFormat::BGRA8;
	};

	struct FCommand
	{
		FMessageHeader Header;
		float Yaw = 0;
		float Pitch = 0;
		float Roll = 0;
		std::uint8_t bCameraFollow = 0;
		std::uint8_t Reserved[7] = {};
	};
	#pragma pack(pop)

	// CommitSequence is odd while the publisher writes and even after commit.
	// Its committed value is exactly FImageFrame::Header::Sequence * 2.
	struct alignas(8) FImageTransportHeader
	{
		std::uint32_t Magic = RMSimSdk::Magic;
		std::uint16_t Version = RMSimSdk::ImageTransportVersion;
		std::uint16_t HeaderBytes = sizeof(FImageTransportHeader);
		std::uint64_t CommitSequence = 0;
	};

	constexpr std::size_t ImageMappingBytes =
		sizeof(FImageTransportHeader) + sizeof(FImageFrame) + ImageBytes;

	static_assert(sizeof(FMessageHeader) == 28, "Protocol header layout changed");
	static_assert(sizeof(FImageFrame) == 48, "Image frame layout changed");
	static_assert(sizeof(FImuSample) == 64, "IMU sample layout changed");
	static_assert(sizeof(FCommand) == 48, "Command layout changed");
	static_assert(sizeof(FImageTransportHeader) == 16, "Image transport layout changed");
	static_assert(offsetof(FImageTransportHeader, CommitSequence) == 8, "Commit sequence must be aligned");
	static_assert(std::is_standard_layout_v<FImageFrame>, "Image frame must be standard layout");
	static_assert(std::is_trivially_copyable_v<FImageFrame>, "Image frame must be byte-copyable");
	static_assert(std::is_trivially_copyable_v<FImageTransportHeader>, "Image transport must be byte-copyable");
}
