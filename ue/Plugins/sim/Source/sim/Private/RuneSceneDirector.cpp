#include "RuneSceneDirector.h"

#include "RMSimWorldSubsystem.h"
#include "Rune.h"

ARuneSceneDirector::ARuneSceneDirector()
{
	PrimaryActorTick.bCanEverTick = true;
	RootComponent = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));
}

void ARuneSceneDirector::BeginPlay()
{
	Super::BeginPlay();
	TArray<FString> Errors;
	bBindingsValid = ValidateBindings(Errors);
	BindingError = FString::Join(Errors, TEXT("; "));

	if (URMSimWorldSubsystem* Subsystem = GetWorld()->GetSubsystem<URMSimWorldSubsystem>())
	{
		Subsystem->RegisterDirector(this);
	}

	ResetMatch(MatchSeed);
}

void ARuneSceneDirector::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);
	if (!bBindingsValid)
	{
		return;
	}

	RedRune->AdvanceRules(DeltaSeconds, bAutoHitTestEnabled, AutoHitDelaySeconds);
	BlueRune->AdvanceRules(DeltaSeconds, bAutoHitTestEnabled, AutoHitDelaySeconds);
	const bool bAnyActivating = RedRune->GetSnapshot().State == ERuneState::Activating
		|| BlueRune->GetSnapshot().State == ERuneState::Activating;
	const FRuneMotionSnapshot& Motion = MotionModel.AdvanceTime(DeltaSeconds, bAnyActivating);
	RedRune->ApplySharedAngle(Motion.AngleRad);
	BlueRune->ApplySharedAngle(Motion.AngleRad);
	if (bDebugOverlay)
	{
		RedRune->DrawDebugTargets();
		BlueRune->DrawDebugTargets();
	}
}

bool ARuneSceneDirector::SetSceneMode(ESceneMode NewMode, int32 Seed)
{
	if (!bBindingsValid)
	{
		return false;
	}

	CurrentMode = NewMode;
	MatchSeed = Seed;
	MotionModel.SetMode(NewMode);
	RedRune->InitializeRules(NewMode, Seed ^ 0x13579BDF);
	BlueRune->InitializeRules(NewMode, Seed ^ 0x2468ACE0);
	return true;
}

bool ARuneSceneDirector::ResetMatch(int32 Seed)
{
	MatchSeed = Seed;
	MotionModel.BeginMatch(Seed);
	return SetSceneMode(CurrentMode, Seed);
}

FRunePairSnapshot ARuneSceneDirector::GetSnapshot() const
{
	FRunePairSnapshot Result;
	Result.Mode = CurrentMode;
	Result.MatchSeed = MatchSeed;
	Result.bBindingValid = bBindingsValid;
	Result.bDebugOverlay = bDebugOverlay;
	Result.bAutoHitTestEnabled = bAutoHitTestEnabled;
	Result.Error = BindingError;
	const FRuneMotionSnapshot& Motion = MotionModel.GetSnapshot();
	Result.SpeedRadPerSecond = Motion.SpeedRadPerSecond;
	Result.AngleRad = Motion.AngleRad;
	if (RedRune)
	{
		Result.Red = RedRune->GetSnapshot();
	}
	if (BlueRune)
	{
		Result.Blue = BlueRune->GetSnapshot();
	}
	return Result;
}

bool ARuneSceneDirector::ValidateBindings(TArray<FString>& OutErrors) const
{
	if (RedRune && BlueRune && RedRune == BlueRune)
	{
		OutErrors.Add(TEXT("RedRune and BlueRune reference the same controller"));
	}
	if (!RedRune)
	{
		OutErrors.Add(TEXT("Missing RedRune controller"));
	}
	else
	{
		RedRune->ValidateBinding(OutErrors);
		if (RedRune->TeamColor != ETeamColor::Red)
		{
			OutErrors.Add(TEXT("RedRune controller has the wrong TeamColor"));
		}
	}
	if (!BlueRune)
	{
		OutErrors.Add(TEXT("Missing BlueRune controller"));
	}
	else
	{
		BlueRune->ValidateBinding(OutErrors);
		if (BlueRune->TeamColor != ETeamColor::Blue)
		{
			OutErrors.Add(TEXT("BlueRune controller has the wrong TeamColor"));
		}
	}
	if (!FixedCamera)
	{
		OutErrors.Add(TEXT("Missing FixedCamera"));
	}
	return OutErrors.IsEmpty();
}
