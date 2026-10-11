#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "RuneSceneValidationCommandlet.generated.h"

UCLASS()
class URuneSceneValidationCommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	URuneSceneValidationCommandlet();
	virtual int32 Main(const FString& Params) override;
};
