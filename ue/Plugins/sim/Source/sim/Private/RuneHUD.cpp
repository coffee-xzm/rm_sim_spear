#include "RuneHUD.h"

#include "Engine/Canvas.h"
#include "RMSimWorldSubsystem.h"

namespace
{
	const TCHAR* StateName(ERuneState State)
	{
		switch (State)
		{
		case ERuneState::Inactive: return TEXT("Inactive");
		case ERuneState::Activating: return TEXT("Activating");
		case ERuneState::Activated: return TEXT("Activated");
		case ERuneState::ActivationFailed: return TEXT("Failed");
		default: return TEXT("Unknown");
		}
	}

	const TCHAR* PhaseName(ERuneRoundPhase Phase)
	{
		switch (Phase)
		{
		case ERuneRoundPhase::AwaitFirstHit: return TEXT("First");
		case ERuneRoundPhase::AwaitSecondHit: return TEXT("Second");
		default: return TEXT("None");
		}
	}
}

void ARuneHUD::DrawHUD()
{
	Super::DrawHUD();
	if (!Canvas || !GetWorld())
	{
		return;
	}
	const URMSimWorldSubsystem* Subsystem = GetWorld()->GetSubsystem<URMSimWorldSubsystem>();
	const FRunePairSnapshot Snapshot = Subsystem ? Subsystem->GetSnapshot() : FRunePairSnapshot();
	float Y = 24.0f;
	DrawLine(FString::Printf(TEXT("Mode: %d  speed: %.3f rad/s"), static_cast<int32>(Snapshot.Mode), Snapshot.SpeedRadPerSecond), 24.0f, Y);
	DrawLine(FString::Printf(TEXT("Red: %s/%s  %d/5  hit=%dmm ring=%d"), StateName(Snapshot.Red.State),
		PhaseName(Snapshot.Red.Phase), Snapshot.Red.ActivatedGroups, Snapshot.Red.LastHitRadiusMm, Snapshot.Red.LastHitRing), 24.0f, Y, FLinearColor::Red);
	DrawLine(FString::Printf(TEXT("Blue: %s/%s  %d/5  hit=%dmm ring=%d"), StateName(Snapshot.Blue.State),
		PhaseName(Snapshot.Blue.Phase), Snapshot.Blue.ActivatedGroups, Snapshot.Blue.LastHitRadiusMm, Snapshot.Blue.LastHitRing), 24.0f, Y, FLinearColor(0.2f, 0.5f, 1.0f));
	if (!Snapshot.bBindingValid)
	{
		DrawLine(Snapshot.Error, 24.0f, Y, FLinearColor::Yellow);
	}
}

void ARuneHUD::DrawLine(const FString& Text, float X, float& Y, const FLinearColor& Color)
{
	FCanvasTextItem Item(FVector2D(X, Y), FText::FromString(Text), GEngine->GetSmallFont(), Color);
	Canvas->DrawItem(Item);
	Y += 22.0f;
}
