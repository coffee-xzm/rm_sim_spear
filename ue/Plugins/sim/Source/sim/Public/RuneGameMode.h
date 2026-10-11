#pragma once

#include "CoreMinimal.h"
#include "GameFramework/GameModeBase.h"
#include "RuneTypes.h"
#include "RuneGameMode.generated.h"

DECLARE_DYNAMIC_MULTICAST_DELEGATE_OneParam(FOnSceneModeChanged, ESceneMode, NewMode);

UCLASS()
class SIM_API ARuneGameMode : public AGameModeBase
{
	GENERATED_BODY()

public:
	ARuneGameMode();

	UFUNCTION(BlueprintCallable, Category = "Rune")
	bool SetSceneMode(ESceneMode NewMode, int32 Seed = 2026);

	UFUNCTION(BlueprintPure, Category = "Rune")
	ESceneMode GetSceneMode() const { return CurrentSceneMode; }

	UPROPERTY(BlueprintAssignable, Category = "Rune")
	FOnSceneModeChanged OnSceneModeChanged;

private:
	UPROPERTY()
	ESceneMode CurrentSceneMode = ESceneMode::Inactive;
};
