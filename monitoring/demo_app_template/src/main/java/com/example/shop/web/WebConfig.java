package com.example.shop.web;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

@Configuration
public class WebConfig implements WebMvcConfigurer {

    private final HandlerLoggingInterceptor handlerLoggingInterceptor;

    public WebConfig(HandlerLoggingInterceptor handlerLoggingInterceptor) {
        this.handlerLoggingInterceptor = handlerLoggingInterceptor;
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(handlerLoggingInterceptor).addPathPatterns("/api/**");
    }
}
