#pragma once

#include "CoreMinimal.h"
#include "GameFramework/HUD.h"
#include "RuneHUD.generated.h"

UCLASS()
class SIM_API ARuneHUD : public AHUD
{
	GENERATED_BODY()

public:
	virtual void DrawHUD() override;

private:
	void DrawLine(const FString& Text, float X, float& Y, const FLinearColor& Color = FLinearColor::White);
};
