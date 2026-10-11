#pragma once

#include "CoreMinimal.h"
#include "RuneTypes.generated.h"

UENUM(BlueprintType)
enum class ESceneMode : uint8
{
	Inactive UMETA(DisplayName = "未激活"),
	SmallRune UMETA(DisplayName = "小能量机关"),
	LargeRune UMETA(DisplayName = "大能量机关"),
};

UENUM(BlueprintType)
enum class ERuneState : uint8
{
	Inactive UMETA(DisplayName = "未激活"),
	Activating UMETA(DisplayName = "正在激活"),
	Activated UMETA(DisplayName = "已激活"),
	ActivationFailed UMETA(DisplayName = "激活失败"),
};

UENUM(BlueprintType)
enum class ERuneRoundPhase : uint8
{
	None,
	AwaitFirstHit,
	AwaitSecondHit,
};

UENUM(BlueprintType)
enum class ERuneTransitionReason : uint8
{
	None,
	ModeChanged,
	RoundStarted,
	FirstHitSucceeded,
	SecondHitSucceeded,
	RoundCompleted,
	ActivationCompleted,
	WrongTarget,
	FirstHitTimedOut,
	FailureRestarted,
	ActivationDisplayCompleted,
	IgnoredDuplicateHit,
	IgnoredInactiveHit,
	InvalidHit,
};

UENUM(BlueprintType)
enum class ETeamColor : uint8
{
	Red UMETA(DisplayName = "红方"),
	Blue UMETA(DisplayName = "蓝方"),
};

UENUM(BlueprintType)
enum class ECameraView : uint8
{
	FreeThirdPerson UMETA(DisplayName = "自由第三人称"),
	FixedCamera UMETA(DisplayName = "固定相机"),
};

UENUM(BlueprintType)
enum class EFlowEffectState : uint8
{
	Off,
	Arrow,
	Full,
};

USTRUCT(BlueprintType)
struct SIM_API FRuneRuleConfig
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	double FirstHitTimeoutSeconds = 2.5;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	double SecondHitWindowSeconds = 1.0;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	double FailureHoldSeconds = 0.5;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	double CompletionFlashSeconds = 1.5;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	double CompletionFlashIntervalSeconds = 0.15;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	int32 RequiredGroups = 5;
};
