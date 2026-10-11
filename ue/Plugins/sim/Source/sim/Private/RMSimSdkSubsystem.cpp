#include "RMSimSdkSubsystem.h"
#include "Engine/World.h"
#include "Common/UdpSocketBuilder.h"
#include "GameFramework/PlayerController.h"
#include "HAL/PlatformAtomics.h"
#include "HAL/PlatformProcess.h"
#include "Interfaces/IPv4/IPv4Endpoint.h"
#include "Misc/DateTime.h"
#include "Misc/ScopeLock.h"
#include "SocketSubsystem.h"
#include "Sockets.h"

uint64 URMSimSdkSubsystem::NowNs()
{
	const FDateTime Now = FDateTime::UtcNow();
	static const int64 UnixEpochTicks = FDateTime(1970, 1, 1).GetTicks();
	return static_cast<uint64>(Now.GetTicks() - UnixEpochTicks) * 100ull;
}

void URMSimSdkSubsystem::OnWorldBeginPlay(UWorld& World)
{
	Super::OnWorldBeginPlay(World);
	OpenTransport();
}

void URMSimSdkSubsystem::Deinitialize()
{
	CloseTransport();
	Super::Deinitialize();
}

void URMSimSdkSubsystem::OpenTransport()
{
	const uint32 Access = FPlatformMemory::ESharedMemoryAccess::Read | FPlatformMemory::ESharedMemoryAccess::Write;
	const FString ImageName = UTF8_TO_TCHAR(RMSimSdk::ImageSharedMemoryName + 1);
	const FString ImuName = UTF8_TO_TCHAR(RMSimSdk::ImuSharedMemoryName + 1);
	ImageMapping = FPlatformMemory::MapNamedSharedMemoryRegion(ImageName, true, Access, RMSimSdk::ImageMappingBytes);
	ImuMapping = FPlatformMemory::MapNamedSharedMemoryRegion(ImuName, true, Access, sizeof(uint64) + sizeof(RMSimSdk::FImuSample) * RMSimSdk::ImuCapacity);
	CommandSocket = FUdpSocketBuilder(TEXT("RMSimSdkCommand")).AsNonBlocking().AsReusable().BoundToEndpoint(FIPv4Endpoint(FIPv4Address::Any, 39001));
	if (ImageMapping)
	{
		FMemory::Memzero(ImageMapping->GetAddress(), RMSimSdk::ImageMappingBytes);
		const RMSimSdk::FImageTransportHeader Header;
		FMemory::Memcpy(ImageMapping->GetAddress(), &Header, sizeof(Header));
	}
}

void URMSimSdkSubsystem::CloseTransport()
{
	if (CommandSocket) { ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(CommandSocket); CommandSocket = nullptr; }
	if (ImageMapping) { FPlatformMemory::UnmapNamedSharedMemoryRegion(ImageMapping); ImageMapping = nullptr; }
	if (ImuMapping) { FPlatformMemory::UnmapNamedSharedMemoryRegion(ImuMapping); ImuMapping = nullptr; }
}

void URMSimSdkSubsystem::Tick(float)
{
	PollCommands();
	PublishImu();
}

void URMSimSdkSubsystem::PollCommands()
{
	if (!CommandSocket) return;
	RMSimSdk::FCommand Command{}; int32 N = 0;
	if (CommandSocket->Recv(reinterpret_cast<uint8*>(&Command), sizeof(Command), N) && N == sizeof(Command) && Command.Header.Magic == RMSimSdk::Magic && Command.Header.Version == RMSimSdk::Version && Command.Header.Type == static_cast<uint16>(RMSimSdk::EMessageType::Command))
	{
		FScopeLock Lock(&CommandMutex); LastCommand = Command; bHasCommand = true;
	}
}

bool URMSimSdkSubsystem::ConsumeCommand(RMSimSdk::FCommand& OutCommand)
{
	FScopeLock Lock(&CommandMutex); if (!bHasCommand) return false; OutCommand = LastCommand; bHasCommand = false; return true;
}

void URMSimSdkSubsystem::PublishImu()
{
	if (!ImuMapping || !GetWorld()) return;
	RMSimSdk::FImuSample Sample{}; Sample.Header.Type = static_cast<uint16>(RMSimSdk::EMessageType::Imu); Sample.Header.Size = sizeof(Sample); Sample.Header.Sequence = ++ImuSequence; Sample.Header.TimestampNs = NowNs();
	if (APlayerController* PC = GetWorld()->GetFirstPlayerController())
	{
		FRotator R; FVector L; PC->GetPlayerViewPoint(L, R); Sample.Yaw = R.Yaw; Sample.Pitch = R.Pitch; Sample.Roll = R.Roll;
		const double T = FPlatformTime::Seconds();
		if (bHavePreviousPose)
		{
			const double Dt = FMath::Max(T - PreviousTimeSeconds, 1e-6); const FVector Velocity = (L - PreviousLocation) / Dt; const FVector LocalAccel = R.UnrotateVector((Velocity - PreviousVelocity) / Dt) / 100.0;
			Sample.AccelForward = LocalAccel.X; Sample.AccelLeft = -LocalAccel.Y; Sample.AccelUp = LocalAccel.Z;
			const FRotator Delta = (R - PreviousRotation).GetNormalized(); Sample.GyroRoll = FMath::DegreesToRadians(Delta.Roll / Dt); Sample.GyroPitch = -FMath::DegreesToRadians(Delta.Pitch / Dt); Sample.GyroYaw = FMath::DegreesToRadians(Delta.Yaw / Dt); PreviousVelocity = Velocity;
		}
		else bHavePreviousPose = true;
		PreviousLocation = L; PreviousRotation = R; PreviousTimeSeconds = T;
	}
	uint8* Base = static_cast<uint8*>(ImuMapping->GetAddress()); const uint64 Index = Sample.Header.Sequence % RMSimSdk::ImuCapacity; FMemory::Memcpy(Base + sizeof(uint64) + Index * sizeof(Sample), &Sample, sizeof(Sample)); FMemory::Memcpy(Base, &Sample.Header.Sequence, sizeof(uint64));
}

void URMSimSdkSubsystem::PublishCameraBGRA(const TArray<uint8>& BGRA, uint32 Width, uint32 Height, uint64 TimestampNs)
{
	const uint64 RequiredBytes = static_cast<uint64>(Width) * Height * 4u;
	if (!ImageMapping
		|| Width == 0
		|| Height == 0
		|| Width > RMSimSdk::ImageWidth
		|| Height > RMSimSdk::ImageHeight
		|| RequiredBytes > RMSimSdk::ImageBytes
		|| RequiredBytes != static_cast<uint64>(BGRA.Num()))
	{
		return;
	}

	RMSimSdk::FImageFrame Frame {};
	Frame.Header.Type = static_cast<uint16>(RMSimSdk::EMessageType::Camera);
	Frame.Header.Size = sizeof(Frame) + static_cast<uint32>(RequiredBytes);
	Frame.Header.Sequence = ++ImageSequence;
	Frame.Header.TimestampNs = TimestampNs;
	Frame.Width = Width;
	Frame.Height = Height;
	Frame.Stride = Width * 4;
	Frame.DataBytes = static_cast<uint32>(RequiredBytes);
	Frame.PixelFormat = RMSimSdk::EPixelFormat::BGRA8;

	uint8* Base = static_cast<uint8*>(ImageMapping->GetAddress());
	auto* Transport = reinterpret_cast<RMSimSdk::FImageTransportHeader*>(Base);
	const uint64 WritingCommit = Frame.Header.Sequence * 2u + 1u;
	const uint64 CompleteCommit = Frame.Header.Sequence * 2u;
	FPlatformAtomics::InterlockedExchange(
		reinterpret_cast<volatile int64*>(&Transport->CommitSequence),
		static_cast<int64>(WritingCommit));

	uint8* FrameAddress = Base + sizeof(RMSimSdk::FImageTransportHeader);
	FMemory::Memcpy(FrameAddress, &Frame, sizeof(Frame));
	FMemory::Memcpy(FrameAddress + sizeof(Frame), BGRA.GetData(), Frame.DataBytes);
	FPlatformMisc::MemoryBarrier();
	FPlatformAtomics::InterlockedExchange(
		reinterpret_cast<volatile int64*>(&Transport->CommitSequence),
		static_cast<int64>(CompleteCommit));
}
