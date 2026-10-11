#pragma once

#include "CoreMinimal.h"
#include "RuneTypes.h"
#include "RuneSnapshot.generated.h"

USTRUCT(BlueprintType)
struct SIM_API FRuneSnapshot
{
	GENERATED_BODY()

	UPROPERTY(BlueprintReadOnly)
	ESceneMode Mode = ESceneMode::Inactive;

	UPROPERTY(BlueprintReadOnly)
	ERuneState State = ERuneState::Inactive;

	UPROPERTY(BlueprintReadOnly)
	ERuneRoundPhase Phase = ERuneRoundPhase::None;

	UPROPERTY(BlueprintReadOnly)
	ERuneTransitionReason LastTransition = ERuneTransitionReason::None;

	UPROPERTY(BlueprintReadOnly)
	int32 ActivatedGroups = 0;

	UPROPERTY(BlueprintReadOnly)
	TArray<int32> LitArmorIndices;

	UPROPERTY(BlueprintReadOnly)
	TArray<int32> CompletedArmorIndices;

	UPROPERTY(BlueprintReadOnly)
	TArray<int32> ArmorHitRings;

	UPROPERTY(BlueprintReadOnly)
	int32 FirstHitArmorIndex = INDEX_NONE;

	UPROPERTY(BlueprintReadOnly)
	int32 SecondHitArmorIndex = INDEX_NONE;

	UPROPERTY(BlueprintReadOnly)
	int32 LastHitArmorIndex = INDEX_NONE;

	UPROPERTY(BlueprintReadOnly)
	int32 LastHitRadiusMm = INDEX_NONE;

	UPROPERTY(BlueprintReadOnly)
	int32 LastHitRing = 0;

	UPROPERTY(BlueprintReadOnly)
	double DeadlineSeconds = 0.0;

	UPROPERTY(BlueprintReadOnly)
	bool bCompletionFlashOn = false;
};

USTRUCT(BlueprintType)
struct SIM_API FRunePairSnapshot
{
	GENERATED_BODY()

	UPROPERTY(BlueprintReadOnly)
	int32 SchemaVersion = 1;

	UPROPERTY(BlueprintReadOnly)
	ESceneMode Mode = ESceneMode::Inactive;

	UPROPERTY(BlueprintReadOnly)
	FRuneSnapshot Red;

	UPROPERTY(BlueprintReadOnly)
	FRuneSnapshot Blue;

	UPROPERTY(BlueprintReadOnly)
	double SpeedRadPerSecond = 0.0;

	UPROPERTY(BlueprintReadOnly)
	double AngleRad = 0.0;

	UPROPERTY(BlueprintReadOnly)
	int32 MatchSeed = 0;

	UPROPERTY(BlueprintReadOnly)
	bool bBindingValid = false;

	UPROPERTY(BlueprintReadOnly)
	bool bDebugOverlay = false;

	UPROPERTY(BlueprintReadOnly)
	bool bAutoHitTestEnabled = false;

	UPROPERTY(BlueprintReadOnly)
	FString Error;
};
