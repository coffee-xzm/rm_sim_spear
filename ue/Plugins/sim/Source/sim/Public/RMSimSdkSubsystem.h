#pragma once

#include "CoreMinimal.h"
#include "HAL/PlatformMemory.h"
#include "Subsystems/WorldSubsystem.h"
#include "RMSimSdkProtocol.h"
#include "RMSimSdkSubsystem.generated.h"

UCLASS()
class SIM_API URMSimSdkSubsystem : public UTickableWorldSubsystem
{
	GENERATED_BODY()
public:
	virtual void OnWorldBeginPlay(UWorld& World) override;
	virtual void Deinitialize() override;
	virtual void Tick(float DeltaSeconds);
	virtual TStatId GetStatId() const override { RETURN_QUICK_DECLARE_CYCLE_STAT(URMSimSdkSubsystem, STATGROUP_Tickables); }
	void PublishCameraBGRA(const TArray<uint8>& BGRA, uint32 Width, uint32 Height, uint64 TimestampNs);
	bool ConsumeCommand(RMSimSdk::FCommand& OutCommand);
	static uint64 NowNs();
private:
	void OpenTransport();
	void CloseTransport();
	void PollCommands();
	void PublishImu();
	uint64 ImageSequence = 0;
	uint64 ImuSequence = 0;
	class FSocket* CommandSocket = nullptr;
	FPlatformMemory::FSharedMemoryRegion* ImageMapping = nullptr;
	FPlatformMemory::FSharedMemoryRegion* ImuMapping = nullptr;
	RMSimSdk::FCommand LastCommand;
	FCriticalSection CommandMutex;
	bool bHasCommand = false;
	bool bHavePreviousPose = false;
	double PreviousTimeSeconds = 0.0;
	FVector PreviousLocation = FVector::ZeroVector;
	FVector PreviousVelocity = FVector::ZeroVector;
	FRotator PreviousRotation = FRotator::ZeroRotator;
};
