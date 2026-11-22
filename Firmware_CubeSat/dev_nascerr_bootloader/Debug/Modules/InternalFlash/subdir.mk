################################################################################
# Automatically-generated file. Do not edit!
# Toolchain: GNU Tools for STM32 (14.3.rel1)
################################################################################

# Add inputs and outputs from these tool invocations to the build variables 
C_SRCS += \
../Modules/InternalFlash/NASCERR_InternalFlash.c 

OBJS += \
./Modules/InternalFlash/NASCERR_InternalFlash.o 

C_DEPS += \
./Modules/InternalFlash/NASCERR_InternalFlash.d 


# Each subdirectory must supply rules for building sources it contributes
Modules/InternalFlash/%.o Modules/InternalFlash/%.su Modules/InternalFlash/%.cyclo: ../Modules/InternalFlash/%.c Modules/InternalFlash/subdir.mk
	arm-none-eabi-gcc "$<" -mcpu=cortex-m4 -std=gnu11 -g3 -DDEBUG -DUSE_HAL_DRIVER -DSTM32L4S5xx -c -I../Core/Inc -I../Modules/Communication -I../Modules/Database -I../Drivers/STM32L4xx_HAL_Driver/Inc -I../Drivers/STM32L4xx_HAL_Driver/Inc/Legacy -I../Drivers/CMSIS/Device/ST/STM32L4xx/Include -I../Drivers/CMSIS/Include -I../Modules/Bootloader -I../Modules/ExternalFlash -I../Modules/InternalFlash -I../Modules/System -I../Modules/Utils -I../Modules/Application -O0 -ffunction-sections -fdata-sections -Wall -fstack-usage -fcyclomatic-complexity -MMD -MP -MF"$(@:%.o=%.d)" -MT"$@" --specs=nano.specs -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -o "$@"

clean: clean-Modules-2f-InternalFlash

clean-Modules-2f-InternalFlash:
	-$(RM) ./Modules/InternalFlash/NASCERR_InternalFlash.cyclo ./Modules/InternalFlash/NASCERR_InternalFlash.d ./Modules/InternalFlash/NASCERR_InternalFlash.o ./Modules/InternalFlash/NASCERR_InternalFlash.su

.PHONY: clean-Modules-2f-InternalFlash

