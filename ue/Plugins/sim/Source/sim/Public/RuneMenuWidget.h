#pragma once

#include "CoreMinimal.h"
#include "Blueprint/UserWidget.h"
#include "RuneTypes.h"
#include "RuneMenuWidget.generated.h"

UCLASS()
class SIM_API URuneMenuWidget : public UUserWidget
{
	GENERATED_BODY()

public:
	UFUNCTION(BlueprintCallable, Category = "Rune|UI")
	void SetSceneMode(ESceneMode NewMode);

	UFUNCTION(BlueprintCallable, Category = "Rune|UI")
	void SetCameraView(ECameraView NewView);

	UFUNCTION(BlueprintCallable, Category = "Rune|UI")
	void ResetMatch();

	UFUNCTION(BlueprintCallable, Category = "Rune|UI")
	void ToggleDebugOverlay();

	UFUNCTION(BlueprintCallable, Category = "Rune|UI")
	void ToggleAutoHitTest();

	UFUNCTION(BlueprintCallable, Category = "Rune|UI")
	void CloseMenu();

protected:
	virtual TSharedRef<SWidget> RebuildWidget() override;
	FText GetStatusText() const;
};
