#include "RuneGameMode.h"

#include "RMSimWorldSubsystem.h"
#include "RuneHUD.h"
#include "RunePawn.h"
#include "RunePlayerController.h"

ARuneGameMode::ARuneGameMode()
{
	DefaultPawnClass = ARunePawn::StaticClass();
	PlayerControllerClass = ARunePlayerController::StaticClass();
	HUDClass = ARuneHUD::StaticClass();
}

bool ARuneGameMode::SetSceneMode(ESceneMode NewMode, int32 Seed)
{
	URMSimWorldSubsystem* Subsystem = GetWorld() ? GetWorld()->GetSubsystem<URMSimWorldSubsystem>() : nullptr;
	if (!Subsystem || !Subsystem->SetSceneMode(NewMode, Seed))
	{
		return false;
	}
	CurrentSceneMode = NewMode;
	OnSceneModeChanged.Broadcast(NewMode);
	return true;
}
